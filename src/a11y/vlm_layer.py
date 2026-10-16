"""VLM 内容层（L3）：在规则层之后，对 U1/U2/A3 做语义打分。

设计原则（接 checker 时定的）：
- **不替换**规则层。规则层 CPR 保持 100% 可复现；这一层只额外给出 quality 分。
- **默认关闭**：只有 ctx["_vlm"]=True 且设了 OPENAI_API_KEY 才跑，否则整个项目
  零网络依赖、测试不受影响。
- **U1 带图**（Qwen 视觉）、**U2+A3 纯文本**（GLM）——分工见 judge_split.py 标定。
- 失败绝不影响规则层：任何异常都返回 status="skip"。

score 0/1/2 → pass/fail 的映射（阈值暂定，待真人 κ 后标定）：
  score >= 1 视为 pass（有东西），score == 0 视为 fail（空话/与图不符）。
"""
from __future__ import annotations

import base64, json, os, re, subprocess, tempfile, time
from pathlib import Path

from .checker import CheckResult

# ── 可配置项（全部通过环境变量设置）──────────────────────────────────
# 文本模型（只读写文字）—— alt-text 生成、代码生成、U2/A3/L3/L4 评分
LLM_BASE_URL = os.environ.get("CHART_LLM_BASE_URL", "")
LLM_API_KEY = os.environ.get("CHART_LLM_API_KEY", os.environ.get("OPENAI_API_KEY", ""))
_TX_MODEL = os.environ.get("CHART_LLM_MODEL", "gpt-4o-mini")

# 视觉模型（能看图）—— G0 门、U1 评分
# 不单独设置时，自动复用文本模型的地址和 Key
VLM_BASE_URL = os.environ.get("CHART_VLM_BASE_URL", LLM_BASE_URL)
VLM_API_KEY = os.environ.get("CHART_VLM_API_KEY", LLM_API_KEY)
_VL_MODEL = os.environ.get("CHART_VLM_MODEL", "gpt-4o")
# 浏览器路径（SVG 转 PNG 截图用），自动检测常见位置
def _detect_edge():
    candidates = [
        os.environ.get("CHART_EDGE_PATH", ""),
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    import warnings
    warnings.warn("未找到浏览器（Edge/Chrome），VLM 截图功能将不可用。"
                  "请设置 CHART_EDGE_PATH 环境变量。")
    return ""
EDGE = _detect_edge()

_G0_RUBRIC = """你能看到这张图。只回答一个问题：图上有没有【真的画出数据标记】
（柱/点/线/扇区等由数据驱动的图形元素）？
- data_marks：1=有实际数据标记；0=空白坐标系/纯文字/只有飘着的数字、没有任何图形
- title/axes/labels：照实记录，仅作报告——缺刻度数值、缺图例、标签重叠
  都【不影响】readable，那不是本检查管的事。
严格返回JSON：
{"readable":1或0,"title":0或1,"axes":0或1,"data_marks":1或0,"labels":0或1,"reason":"简短中文说明"}
readable 必须等于 data_marks。"""

SEMANTIC_IDS = {"U1", "U2", "C1", "A3"}  # U3 已降级 advisory，不再需要裁判

_U1_RUBRIC = """你能看到图。评"读图说明"0/1/2：
0=空话或与图不符/误导；1=说了图大概是什么，但没教怎么读；
2=和图对得上，且真的教了怎么读。含糊的"展示了XX分布"只算1。
返回JSON {"score":int}。"""
_TX_RUBRIC = """只看文字与CSV，评四项0/1/2：
标题0=占位/跑题；1=点主题但笼统；2=对象+维度/单位。
结论0=与CSV矛盾或编造；1=提真实内容但浅；2=正确说真实趋势/最值。
感知现象0=描述了CSV中根本不存在的聚类/离群/缺口；1=沾边但浅（如"有波动"）；
2=准确点出CSV支持的真实形态（哪个区域高、哪两个点离群、分布偏哪边）。
领域解读0=把推测说成事实、无"suggests"标记或过度声称因果；
1=有推测标记但与用户问题关联很弱；2=以"This suggests"开头且直接回应用户问题。
字段名固定，返回JSON {"title_quality":int,"takeaway_accuracy":int,
"l3_patterns_accuracy":int,"l4_context_caveated":int}。"""


def _client(timeout: int = 120, kind: str = "text"):
    """kind="text" → 文本模型客户端；kind="vision" → 视觉模型客户端。"""
    from openai import OpenAI
    if kind == "vision":
        url, key = VLM_BASE_URL, VLM_API_KEY
    else:
        url, key = LLM_BASE_URL, LLM_API_KEY
    if not url:
        raise RuntimeError(
            f"{'视觉模型' if kind == 'vision' else 'LLM'}层未配置："
            f"请设置环境变量 CHART_{'VLM' if kind == 'vision' else 'LLM'}_BASE_URL "
            f"和对应 API Key。不设时规则检查仍可正常运行。")
    if not key:
        raise RuntimeError(
            f"{'视觉模型' if kind == 'vision' else 'LLM'}层未配置："
            f"请设置对应的 API Key 环境变量。")
    return OpenAI(base_url=url, api_key=key, timeout=timeout)


def _svg_to_png(svg_path: Path, png: Path) -> None:
    if not EDGE:
        return
    png = png.resolve()
    if png.exists() and png.stat().st_size > 1000:
        return
    txt = Path(svg_path).read_text(encoding="utf-8", errors="ignore")
    w, h = 460, 320
    m = re.search(r'viewBox="[\d.\s-]*?([\d.]+)\s+([\d.]+)"', txt)
    if m:
        w, h = int(float(m.group(1))), int(float(m.group(2)))
    win = f"{w+24},{h+24}"
    url = svg_path.resolve().as_uri()
    # 每进程独立 profile：并发跑批时共享目录会导致浏览器抢锁、截图失败
    prof = (Path(tempfile.gettempdir()) / f"browser_headless_profile_{os.getpid()}").resolve()
    subprocess.run([EDGE, "--headless", "--disable-gpu",
                    f"--user-data-dir={prof}", f"--screenshot={png}",
                    f"--window-size={win}", url], capture_output=True, timeout=60)


def _write_tmp_svg(svg: str) -> Path:
    p = Path(tempfile.gettempdir()) / ("g0_" + str(abs(hash(svg))) + ".svg")
    p.write_text(svg, encoding="utf-8")
    return p


def vlm_legibility_gate(svg: str, ctx: dict, svg_path: str | None = None):
    """G0 前置可读门。返回 (CheckResult|None, readable: bool|None)。"""
    if not (ctx or {}).get("_vlm") or not os.environ.get("OPENAI_API_KEY"):
        return None, None
    try:
        c = _client(kind="vision")
        sp = Path(svg_path) if svg_path else _write_tmp_svg(svg)
        png = Path(tempfile.gettempdir()) / ("g0png_" + str(abs(hash(svg))) + ".png")
        if png.exists():
            png.unlink()
        _svg_to_png(sp, png)
        if not png.exists() or png.stat().st_size < 1000:
            return CheckResult("G0", "VLM legibility gate", True, "VLM", "skip",
                               "截图失败，G0 跳过"), None
        b64 = base64.b64encode(png.read_bytes()).decode()
        import hashlib as _h
        ck = "g0|" + _h.sha1((_G0_RUBRIC + svg).encode("utf-8", "ignore")).hexdigest()
        raw = _call(c, _VL_MODEL, [
            {"role": "system", "content": _G0_RUBRIC},
            {"role": "user", "content": [
                {"type": "text", "text": "判断这张图是否包含数据标记。"},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{b64}"}}]}], 500,
            cache_key=ck)
        m = re.search(r"\{[\s\S]*\}", raw)
        d = json.loads(m.group(0)) if m else {}
        # 2026-10-15 口径 (a)：门只判 data_marks（R0 的视觉版）；
        # title/axes/labels 照实记录进 details 作 advisory，不影响判定。
        # 依据：严格版 rubric 正常对照误杀 3/6（缺刻度/无图例），
        # 见 docs/问题清单 第 4 条与 logs/blind/g0_result.txt。
        readable = bool(d.get("data_marks", d.get("readable", 0)))
        comps = {k: d.get(k) for k in ("title", "axes", "data_marks", "labels")}
        reason = d.get("reason", "")
        if readable:
            r = CheckResult("G0", "VLM legibility gate", True, "VLM", "pass",
                            f"VLM 判定数据标记存在（门只判此项；刻度/图例"
                            f"细节另报）：{reason}", comps)
        else:
            r = CheckResult("G0", "VLM legibility gate", True, "VLM", "fail",
                            f"VLM 判定无数据标记（空图）：{reason}", comps)
        return r, readable
    except Exception as exc:
        return CheckResult("G0", "VLM legibility gate", True, "VLM", "skip",
                           f"G0 异常: {exc!r}"[:120]), None


_CACHE_FILE = Path(__file__).resolve().parent.parent.parent / "logs" / ".vlm_cache.json"

def _call(c, model, msgs, tokens=600, cache_key: str | None = None,
          extra_body: dict | None = None,
          complete_flag: "callable | None" = None):
    """temperature=0 + 磁盘缓存：同图重跑零 API 消耗，结果完全可复现。
    默认键 = 模型+全部消息的 sha1；带截图的消息请显式传 cache_key
    （用 SVG 源文本等稳定内容做键）——Edge 每次渲染的 PNG 字节不完全一致，
    用消息做键会每次缓存 miss，还导致同图判定翻转（2026-10-15 实测）。
    缓存文件不入库（.gitignore 已排除）。"""
    # 注意：调用参数（tokens/thinking 挡位）变更后，旧参数写入的条目仍会被
    # 命中——包括截断的坏响应（2026-10-15 踩坑：截断 gen 条目被反复命中，
    # 假低 CPR 54.5%）。改参数后应删掉对应前缀的旧条目。
    import hashlib
    key = None
    cache = {}
    try:
        key = cache_key or hashlib.sha1(json.dumps(
            {"m": model, "msgs": msgs}, sort_keys=True,
            ensure_ascii=False).encode()).hexdigest()
        if not cache_key:
            key = hashlib.sha1(("msgs|" + key).encode()).hexdigest()
        if _CACHE_FILE.exists():
            cache = _load_cache()
        if key in cache:
            cached = cache[key]
            # 完整性守卫：截断响应曾被永久缓存反复命中（2026-10-15 踩坑）。
            # complete_flag 不满足时不返回，也顺手清掉这条坏缓存。
            if complete_flag is None or complete_flag(cached):
                return cached
            try:
                cache.pop(key)
                _save_cache(cache)
            except Exception:
                pass
    except Exception:
        key = None
    for _ in range(3):
        kwargs = {"model": model, "temperature": 0,
                  "max_tokens": tokens, "messages": msgs}
        if extra_body:
            # 走 SDK 的 extra_body：服务端私有参数（如 GLM 的 thinking）原样进 JSON body
            kwargs["extra_body"] = extra_body
        r = c.chat.completions.create(**kwargs)
        raw = r.choices[0].message.content or ""
        if raw.strip() and (complete_flag is None or complete_flag(raw)):
            if key:
                try:
                    cache[key] = raw
                    _save_cache(cache)
                except Exception:
                    pass
            return raw
    return ""


def _load_cache() -> dict:
    """容错读：并发写坏/半截文件直接当空缓存重建。"""
    try:
        return json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_cache(cache: dict) -> None:
    """原子写（临时文件 + os.replace）：并发进程写坏缓存的事故实测过。"""
    import os
    _CACHE_FILE.parent.mkdir(exist_ok=True)
    tmp = _CACHE_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, _CACHE_FILE)


_JUDGE_NAMES = {"U1": "Purpose (VLM)", "U2": "Title (VLM)",
                "A3": "Takeaway (VLM)", "L3": "Patterns (VLM)",
                "L4": "Context (VLM)"}

def _judge_result(cid: str, score, note: str, *, na_reason: str = "") -> CheckResult:
    """0/1/2 分 → CheckResult。score>=1 pass，0 fail，None 按原因 skip/na。

    critical=False：LLM 质量层是 advisory（需求 8：不进 critical 集、
    不影响规则 CPR）——compute_cpr 按 layer=="VLM" 整层排除。
    """
    if score is None:
        status = "na" if na_reason else "skip"
        reason = na_reason or "VLM 评分失败/无图"
        return CheckResult(cid, _JUDGE_NAMES[cid], False, "VLM", status, reason)
    status = "pass" if score >= 1 else "fail"
    return CheckResult(cid, _JUDGE_NAMES[cid], False, "VLM", status,
                       f"VLM 质量分={score}/2（{note}）", {"vlm_score": score})

def _score_to_result(cid: str, name: str, score, note: str) -> CheckResult:
    """旧接口保留（实验脚本在用）。"""
    if score is None:
        return CheckResult(cid, name, True, "VLM", "skip", "VLM 评分失败/无图")
    status = "pass" if score >= 1 else "fail"
    return CheckResult(cid, name, True, "VLM", status,
                       f"VLM 质量分={score}/2（{note}）", {"vlm_score": score})


def run_vlm_judge(svg: str, ctx: dict, svg_path: str | None = None) -> list[CheckResult]:
    """LLM 语义质量层：U1/U2/A3/L3/L4 五项 0/1/2 打分（2026-10-16 接入主流程）。

    - U1 带图（Qwen 视觉，1 次调用）；U2/A3/L3/L4 纯文本合并成 1 次调用（GLM），
      加上 G0 门共 3 次/图，为成本上限（需求 6.3：文本合并省 round-trip）。
    - L3/L4 文本为空（deterministic 兜底场景）→ 标 na，不采信 LLM 对空串的评分。
    - 失败绝不影响规则层：任何一段异常只把该项标 skip。
    - ctx["_vlm"] 未设 → 返回 []（LLM 层未开启，不产出任何项）；
      开了但无 OPENAI_API_KEY → 五项全 skip（run_goal2 的 cpr_llm 里可见）。
    """
    ids = ("U1", "U2", "A3", "L3", "L4")
    if not (ctx or {}).get("_vlm"):
        return []
    if not LLM_API_KEY:
        return [CheckResult(i, _JUDGE_NAMES[i], False, "VLM", "skip",
                            "LLM 质量层开启但未配置 API Key") for i in ids]
    out: list[CheckResult] = []

    # --- U1 带图（视觉模型客户端，独立 try：一段失败不连累文本四项） ---
    try:
        vc = _client(kind="vision")
        out.append(_judge_u1(svg, ctx, svg_path, vc))
    except Exception as exc:
        out.append(CheckResult("U1", _JUDGE_NAMES["U1"], False, "VLM", "skip",
                               f"U1 评分异常: {exc!r}"[:120]))

    # --- U2/A3/L3/L4 纯文本（文本模型客户端，一次调用） ---
    try:
        tc = _client(kind="text")
        out.extend(_judge_texts4(ctx, tc))
    except Exception as exc:
        out.extend(CheckResult(i, _JUDGE_NAMES[i], False, "VLM", "skip",
                               f"文本评分异常: {exc!r}"[:120])
                   for i in ("U2", "A3", "L3", "L4"))
    return out

def _judge_u1(svg: str, ctx: dict, svg_path: str | None, c) -> CheckResult:
    """U1 读图说明：Qwen 视觉模型看图 + readout_note。"""
    note = (ctx.get("readout_note") or "").strip()
    if not note:
        return _judge_result("U1", None, "", na_reason="readout_note 为空，无内容可审")
    # 优先复用 G0 门截过的同一张 PNG（同图同截图，省一次 Edge 调用；
    # G0 已截图时即使 svg_path 未传也可评，不必强依赖落盘路径）
    png = Path(tempfile.gettempdir()) / ("g0png_" + str(abs(hash(svg))) + ".png")
    if not (png.exists() and png.stat().st_size > 1000):
        if not (svg_path and Path(svg_path).exists()):
            return _judge_result("U1", None, "", na_reason="SVG 未落盘，无法截图")
        png = Path(tempfile.gettempdir()) / ("a11y_" + str(abs(hash(svg_path))) + ".png")
        _svg_to_png(Path(svg_path), png)
    if not (png.exists() and png.stat().st_size > 1000):
        return _judge_result("U1", None, "", na_reason="截图失败")
    b64 = base64.b64encode(png.read_bytes()).decode()
    import hashlib as _h
    ck = ("u1|" + _h.sha1(
        (_U1_RUBRIC + note + svg).encode("utf-8", "ignore")).hexdigest())
    raw = _call(c, _VL_MODEL, [
        {"role": "system", "content": _U1_RUBRIC},
        {"role": "user", "content": [
            {"type": "text", "text": f"读图说明: {note}"},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b64}"}}]}], 500,
        cache_key=ck)
    m = re.search(r"\{[\s\S]*\}", raw)
    u1 = json.loads(m.group(0)).get("score") if m else None
    if not isinstance(u1, int):
        u1 = None
    return _judge_result("U1", u1, "带图")

def _judge_texts4(ctx: dict, c) -> list[CheckResult]:
    """U2/A3/L3/L4：GLM 文本模型一次调用合并评分（不看图，省视觉成本）。

    L3/L4 文本为空（确定性兜底场景）→ 直接标 na，不采信 LLM 对空串的评分；
    合并调用照常发出（U2/A3 仍要评），空的字段以 "(空)" 占位保持
    消息结构稳定——同图同输入的缓存 key 不因 L3/L4 有无而分叉。
    """
    title = (ctx.get("title") or "").strip()
    takeaway = (ctx.get("takeaway") or "").strip()
    l3 = (ctx.get("l3_patterns") or "").strip()
    l4 = (ctx.get("l4_context") or "").strip()
    csv_text = (ctx.get("figure_csv") or "").strip()
    question = (ctx.get("question") or "").strip()
    import hashlib as _h
    ck = "tx4|" + _h.sha1((_TX_RUBRIC + title + takeaway + l3 + l4
                           + csv_text + question).encode("utf-8", "ignore")).hexdigest()
    raw = _call(c, _TX_MODEL, [
        {"role": "system", "content": _TX_RUBRIC},
        {"role": "user", "content":
            f"CSV:\n{csv_text or '(空)'}\n\n标题: {title or '(空)'}\n"
            f"结论: {takeaway or '(空)'}\n感知现象描述(L3): {l3 or '(空)'}\n"
            f"领域解读(L4): {l4 or '(空)'}\n用户原始问题: {question or '(空)'}"}],
        600, cache_key=ck)
    m = re.search(r"\{[\s\S]*\}", raw)
    d = json.loads(m.group(0)) if m else {}

    def _int(x):
        return x if isinstance(x, int) else None

    out = [_judge_result("U2", _int(d.get("title_quality")), "纯文本"),
           _judge_result("A3", _int(d.get("takeaway_accuracy")), "纯文本")]
    # L3/L4 空 → na（无内容可审），有内容才采信 LLM 分数
    out.append(_judge_result("L3", _int(d.get("l3_patterns_accuracy")) if l3 else None,
                             "纯文本",
                             na_reason="" if l3 else "L3 为空（确定性兜底），无内容可审"))
    out.append(_judge_result("L4", _int(d.get("l4_context_caveated")) if l4 else None,
                             "纯文本",
                             na_reason="" if l4 else "L4 为空（确定性兜底），无内容可审"))
    return out
