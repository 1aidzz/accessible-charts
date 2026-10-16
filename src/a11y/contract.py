"""模型输出契约：让 agent 除代码外再产出「三件套」，并把它写进 SVG。

为什么要有这个文件
------------------
CHARTABILITY_MAPPING.md §3a：13 条 MAIN 里有 4 条（U1 读图说明 / U2 标题 /
A3 趋势描述 / C1 数据表）判的是**图以外的东西**。agent 只吐 matplotlib 代码时，
这 4 条永远挂，且模型再怎么改代码也无解。所以必须扩展输出契约。

三件套 = title + readout_note + takeaway + data_table_csv（叫"三件套"是因为
title 通常已经画在图里，真正新增的是后三个）。

分组口径（§3b，写死不要改）
--------------------------
- `generated`：内容**必须来自模型**。我们只做格式搬运（把文本写进 SVG 的
  <title>/<desc> 节点、把 CSV 存盘）。模型没产 → 判 fail。
- `injected`：模型没产时，用确定性规则兜底（title ← 图内最大字号文本，
  desc ← takeaway 或 nl_query，table ← 从 figure 反解的绘图数据）。
  这一组是**能力上界参考**，不是模型真实水平。报告里必须分开讲。

本文件只做三件事：拼提示词、解析模型输出、把三件套注入 SVG。
判定逻辑在 checker.py，数据反解在 extract.py。
"""

from __future__ import annotations

import html
import re

# ============================================================ 提示词片段

# --- (1) 代码层面的无障碍要求：加在原有的 ours 提示词后面 ---
A11Y_CODE_RULES = """Accessibility requirements for the figure itself:
- Text size: every piece of text (title, axis labels, tick labels, legend) must be at least 12pt. \
Set them explicitly, e.g. ax.set_title(..., fontsize=14), ax.set_xlabel(..., fontsize=12), \
ax.tick_params(labelsize=12), ax.legend(fontsize=12).
- Colorblind-safe palette: if the chart encodes categories by color, use this Okabe-Ito palette \
in order: '#0072B2' (blue), '#E69F00' (orange), '#009E73' (green), '#CC79A7' (pink), \
'#56B4E9' (sky blue), '#D55E00' (vermillion), '#F0E442' (yellow), '#000000' (black). \
Never use red/green or blue/orange pairs as the only distinction.
- Do not rely on color alone: add direct value labels, distinct markers, or hatch patterns \
so categories can be told apart in grayscale.
- Contrast: some of the palette colors above are LIGHT (especially the orange and yellow) \
and have poor contrast on a white background. Always give filled shapes a dark outline, \
e.g. bar(..., edgecolor='black', linewidth=0.8) or pie(..., wedgeprops={'edgecolor': 'black', \
'linewidth': 0.8}). Never use light-gray text.
"""

# --- (2) 输出契约：要求模型在代码块之后再给一个元数据块 ---
# 用 === 分隔而不是 JSON，是因为 data_table_csv 是多行 CSV，塞 JSON 要转义换行，
# 小模型很容易转义错；纯文本分段最稳。
A11Y_OUTPUT_SPEC = """IMPORTANT: close the python code fence FIRST, then append the accessibility \
metadata block OUTSIDE and AFTER the fence — never put it inside the code block \
(it would break the code). Exactly this shape:

===A11Y===
TITLE: <a specific title stating what the chart shows and the main conclusion; \
never write "Chart of X vs Y">
READOUT: <1-2 plain sentences telling a reader who cannot see the chart what it is for \
and how to read it>
TAKEAWAY: <1-2 sentences naming the extreme values / trend / comparison that a sighted \
reader notices immediately>
TABLE:
<the data actually plotted, as CSV: one header row, then one row per data point, max 30 rows>
===END===

Every field is mandatory. TABLE must contain the numbers that the chart really plots. \
Keep the reading level at grade 9 or below: short sentences (under 15 words each) and \
common words — avoid long words like "distribution", "percentage", "respectively", \
"predominantly"."""


# --- (3) 自修复反馈：代码能跑但无障碍检查没过时回传给模型 ---
A11Y_REPAIR_PROMPT = """Your code runs, but the figure fails {n} accessibility check(s) \
(Chartability heuristics):

{fails}

Rewrite the FULL code so those issues are fixed, and fill in the ===A11Y=== block again.
Reply with ONE fenced python code block followed by the ===A11Y=== block."""


def format_failures(failures) -> str:
    """把 CheckResult 列表格式化成给模型看的清单。"""
    lines = []
    for r in failures:
        tag = "[critical]" if r.critical else ""
        lines.append(f"- {r.id} {r.name} {tag}: {r.reason}")
    return "\n".join(lines)


# ============================================================ 解析

A11Y_BLOCK = re.compile(r"===A11Y===(.*?)===END===", re.DOTALL | re.IGNORECASE)

# 字段行：TITLE: / READOUT: / TAKEAWAY: / TABLE:
_FIELD_RE = re.compile(
    r"^(?P<key>TITLE|READOUT|TAKEAWAY|TABLE)\s*:\s?(?P<val>.*)$",
    re.IGNORECASE | re.MULTILINE,
)

_EMPTY_FIELDS = {"title": "", "readout_note": "", "takeaway": "", "data_table_csv": ""}

_KEY_MAP = {
    "TITLE": "title",
    "READOUT": "readout_note",
    "TAKEAWAY": "takeaway",
    "TABLE": "data_table_csv",
}


def _clean_csv(text: str) -> str:
    """洗掉模型可能加的代码围栏 / 缩进 / 空行。"""
    if not text:
        return ""
    # 去掉 ```csv ... ``` 围栏
    text = re.sub(r"^```(?:csv|text|txt)?[ \t]*\r?\n", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\r?\n?```$", "", text.strip())
    lines = [ln.strip() for ln in text.splitlines()]
    # 去掉首尾空行，但保留内部的（CSV 不该有空行，只是容错）
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _clean_text(text: str) -> str:
    if not text:
        return ""
    s = " ".join(text.split())
    # 模型偶尔会把整个字段写成 "..." 占位
    if s.strip(".<>[]()\"' ") == "" or s.lower() in {"n/a", "none", "null", "todo"}:
        return ""
    return s


def parse_a11y_fields(text: str) -> dict:
    """从模型输出里解析三件套。

    容错设计：优先认 ===A11Y===...===END=== 块；模型写歪了（比如用了别的分隔符）
    就退化为全文搜 `TITLE:` 这类字段行——宁可解析得宽松，也不要把模型的产出
    误判成"没产出"（那会让 generated 组的分被低估，且无法区分是哪个环节的问题）。
    解析不出来的字段留空字符串，由 checker 判 fail。
    """
    out = dict(_EMPTY_FIELDS)
    if not text:
        return out

    m = A11Y_BLOCK.search(text)
    body = m.group(1) if m else text

    hits = list(_FIELD_RE.finditer(body))
    for i, hit in enumerate(hits):
        key = _KEY_MAP[hit.group("key").upper()]
        # 起点必须是**值**的起点，不是整行的末尾（用 hit.end() 会把单行字段截成空串、
        # 并让 TABLE 丢掉表头行）。
        start = hit.start("val")
        # 字段的值一直延续到下一个字段行（TABLE 是多行的）
        end = hits[i + 1].start() if i + 1 < len(hits) else len(body)
        val = body[start:end]
        out[key] = _clean_csv(val) if key == "data_table_csv" else _clean_text(val)
    return out


def has_any_field(fields: dict) -> bool:
    return any((fields.get(k) or "").strip() for k in _EMPTY_FIELDS)


# ============================================================ CSV 质量（advisory）

def csv_quality(csv_text: str) -> dict:
    """数据表质量体检。**advisory，不入 CPR 分母**（口径见 mapping §5-2）。

    C1 这条 MAIN 只判"有没有表"；这里额外记录"表像不像样"，
    用来回答报告里必然会被追问的一句：模型给的表是不是瞎编的。
    """
    if not (csv_text or "").strip():
        return {"rows": 0, "cols": 0, "consistent": False, "has_header": False}
    lines = [ln for ln in csv_text.splitlines() if ln.strip()]
    if not lines:
        return {"rows": 0, "cols": 0, "consistent": False, "has_header": False}
    widths = [len([c for c in ln.split(",")]) for ln in lines]
    consistent = len(set(widths)) == 1 and widths[0] >= 2
    # 表头：首行至少有一列不是纯数字
    header = lines[0].split(",")
    has_header = any(not _is_number(c.strip()) for c in header)
    return {
        "rows": len(lines),
        "cols": widths[0] if widths else 0,
        "consistent": consistent,
        "has_header": has_header,
    }


def _is_number(s: str) -> bool:
    try:
        float(s.replace("%", "").replace(",", ""))
        return True
    except (TypeError, ValueError):
        return False


# ============================================================ SVG 注入

_SVG_OPEN = re.compile(r"<svg\b", re.IGNORECASE)
_FIGURE_OPEN = re.compile(r'<g\s+id="figure_1"[^>]*>', re.IGNORECASE)


def inject_svg_metadata(svg: str, title: str = "", desc: str = "") -> str:
    """把 role=img + <title> + <desc> 写进 SVG。

    为什么必须注入：P2（content is only visual）要求 SVG 自带无障碍语义，
    而 matplotlib 的 SVG 后端不会写这些节点。这一步是"格式搬运"——
    内容来自模型（generated）或来自兜底规则（injected），
    搬运本身两组都做，否则测的是"matplotlib 会不会写 ARIA"而不是模型。

    ⚠ **插入位置很关键（2026-10-02 trade-off 验证踩出来的坑）**：
    title/desc 必须放在 `<g id="figure_1">` **内部**，不能放在 `<svg>` 根节点下。
    viseval 官方的 deconstruction 反解器把 `<svg>` 的第一个子节点当成图，
    title 排在前面时它会读到 title、认为图是空的 → 判 "Empty figure"
    → legality 全挂（实测 100 个 instance 的 deconstruction 失败率 96.3%）。
    放进 figure 组内之后，官方反解器能正常识别 subplot，我们的 P2 也能照常通过
    （P2 只看节点存不存在，与位置无关）。
    """
    if not svg:
        return svg
    title = html.escape((title or "").strip())
    desc = html.escape((desc or "").strip())
    # 已经注入过就先清掉旧的（无论它在根下还是 figure 组内），避免重复
    svg = re.sub(r"<title\b[^>]*>.*?</title>", "", svg, flags=re.DOTALL | re.IGNORECASE)
    svg = re.sub(r"<desc\b[^>]*>.*?</desc>", "", svg, flags=re.DOTALL | re.IGNORECASE)
    head = '<svg role="img" aria-labelledby="a11yTitle a11yDesc"'
    svg = _SVG_OPEN.sub(head, svg, count=1)
    block = ""
    if title:
        block += f'<title id="a11yTitle">{title}</title>'
    if desc:
        block += f'<desc id="a11yDesc">{desc}</desc>'
    if not block:
        return svg
    # 优先放进 figure 组；结构不认（理论上不会）才回退到根节点下
    return _insert_after_tag(svg, _FIGURE_OPEN, block) or _insert_after_svg_open(svg, block)


def _insert_after_tag(svg: str, pattern: re.Pattern, block: str) -> str | None:
    """把 block 插到 pattern 匹配到的开标签之后。匹配不到返回 None。"""
    if not block:
        return None
    m = pattern.search(svg)
    if not m:
        return None
    close = svg.find(">", m.end())
    if close == -1:
        return None
    return svg[: close + 1] + block + svg[close + 1 :]


def _insert_after_svg_open(svg: str, block: str) -> str:
    """把 block 插到 `<svg ...>` 开标签结束之后。"""
    if not block:
        return svg
    m = _SVG_OPEN.search(svg)
    if not m:
        return svg
    close = svg.find(">", m.end())
    if close == -1:
        return svg
    return svg[: close + 1] + block + svg[close + 1 :]


# ============================================================ injected 兜底

def fallback_title(svg: str, nl_query: str = "") -> str:
    """injected 组兜底标题：优先用图里字号最大的文本（通常就是模型写的 title）。"""
    try:
        from checker import _extract_text_contents  # 包内相对模块
    except ImportError:  # 包外直接 import 时兜底
        from .checker import _extract_text_contents
    texts = _extract_text_contents(svg)
    if texts:
        return texts[0]
    return (nl_query or "").strip().rstrip(".") or "Untitled chart"


def fallback_desc(takeaway: str, readout: str, nl_query: str, chart: str = "") -> str:
    """injected 组兜底描述：有什么用什么，顺序 takeaway > readout > nl_query。"""
    if (takeaway or "").strip():
        return takeaway.strip()
    if (readout or "").strip():
        return readout.strip()
    q = (nl_query or "").strip()
    if chart:
        return f"{chart} chart. {q}".strip()
    return q or "No description available."


def build_injected(fields: dict, *, svg: str, nl_query: str,
                   chart: str, data_table_csv: str) -> tuple[dict, list[str]]:
    """把 generated 的三件套补齐成 injected 版本。

    返回 (补齐后的字段, 本次补了哪些字段)。补了哪些要记下来——报告里要能说清
    "injected 组的分数里有多少是我们替它做的"。
    """
    out = dict(fields)
    filled = []
    if not (out.get("title") or "").strip():
        out["title"] = fallback_title(svg, nl_query)
        filled.append("title")
    readout = (out.get("readout_note") or "").strip()
    takeaway = (out.get("takeaway") or "").strip()
    if not readout:
        out["readout_note"] = fallback_desc(takeaway, "", nl_query, chart)
        filled.append("readout_note")
    if not takeaway:
        out["takeaway"] = fallback_desc("", readout, nl_query, chart)
        filled.append("takeaway")
    if not (out.get("data_table_csv") or "").strip():
        out["data_table_csv"] = (data_table_csv or "").strip()
        if out["data_table_csv"]:
            filled.append("data_table_csv")
    return out, filled
