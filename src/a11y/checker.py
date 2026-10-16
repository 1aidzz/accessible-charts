"""Chartability 无障碍检查器 —— 目标一 Phase 2 产出。

输入：一张图的 SVG 文本（matplotlib 生成）+ 可选的扩展契约字段（title / readout_note /
      takeaway / data_table_csv）。
输出：13 条 MAIN 启发式的逐条判定结果 + CPR 分数。

设计原则
--------
1. 纯确定性优先：能用规则判的绝不调模型（L1/L2 层先跑，L3 VLM 层最后）。
2. 不适用绝不默认通过：和 viseval 的 all([])==True 坑反着来 —— n/a 从分子分母同时剔除。
3. 每个检查函数自带 ID / critical 标记 / layer 标记，方便聚合和排序。
4. 解析靠正则不用 ElementTree：matplotlib SVG 有 namespace 和注释，ElementTree 会丢注释
   （而文本内容恰恰在 <!-- ... --> 里），正则更可控。

依赖
----
只依赖 **textstat**（阅读等级）。CVD 仿真与色差全部走本模块内的纯 Python 实现
（``_simulate_cvd`` / ``_delta_e_lab``），**不再 import colorspacious**。

⚠ 为什么必须留着这条警告：本机 `colorspacious` 在 numpy 2.x 下
（实测 numpy 2.4.6）**import 即段错误**，try/except 兜不住，症状是
"无输出、退出码异常、日志 0 字节"。曾经为了让 `import colorspacious` 不炸，
我们把 CVD 那部分改成纯 Python。**别再把 colorspacious import 回来。**
（2026-10-03 探针踩过一次：探针脚本自己在主进程 import 它，整个脚本静默死，
连 print 都吐不出来——所以这个依赖只能在子进程里探。）
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from typing import Callable

# ============================================================ 数据模型

@dataclass
class CheckResult:
    """单条启发式的判定结果。"""
    id: str               # "P1", "P3", "U2" ...
    name: str             # "Low contrast"
    critical: bool        # Chartability 标记的 critical
    layer: str            # "RULE" | "CVD" | "VLM"
    status: str           # "pass" | "fail" | "na" | "skip"
    reason: str           # 人话解释，可直接回传给模型做自修复
    details: dict = field(default_factory=dict)


@dataclass
class CPR:
    """CPR 汇总指标。"""
    cpr_all: float | None        # 通过条数 / 适用条数（None = 没有适用条）
    cpr_critical: float | None   # critical 子集
    total: int                   # 全部 13 条
    applicable: int              # 进分母的条数
    passed: int
    failed: int
    na: int                      # 不适用（从分母剔除）
    skip: int                    # 检查器自身出错
    failures: dict               # {heuristic_id: reason}

    def __str__(self) -> str:
        a = f"{self.cpr_all:.1%}" if self.cpr_all is not None else "N/A"
        c = f"{self.cpr_critical:.1%}" if self.cpr_critical is not None else "N/A"
        return (f"CPR={a}  critical={c}  "
                f"passed={self.passed}/{self.applicable}  "
                f"na={self.na}  skip={self.skip}")


# ============================================================ 颜色工具

_NAMED_COLORS = {
    "white": (255, 255, 255), "black": (0, 0, 0),
    "red": (255, 0, 0), "green": (0, 128, 0), "blue": (0, 0, 255),
    "yellow": (255, 255, 0), "orange": (255, 165, 0),
    "gray": (128, 128, 128), "grey": (128, 128, 128),
    "none": None, "transparent": None,
}

def _parse_color(s: str) -> tuple[int, int, int] | None:
    """hex / rgb() / CSS named → (R, G, B)。不认识返回 None。"""
    s = s.strip().lower()
    if s in _NAMED_COLORS:
        return _NAMED_COLORS[s]
    if s.startswith("#"):
        h = s[1:]
        if len(h) == 3:
            h = h[0]*2 + h[1]*2 + h[2]*2
        if len(h) == 6:
            return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    m = re.match(r"rgba?\(\s*(\d+)[,\s]+(\d+)[,\s]+(\d+)", s)
    if m:
        return (int(m[1]), int(m[2]), int(m[3]))
    return None


def _extract_colors(svg: str, props=("fill", "stroke")) -> list[tuple[int, int, int]]:
    """从 SVG 里抽取 fill / stroke 颜色（去重，去掉 none / 白色背景）。

    props 可只给 ("fill",)：P1 判"数据元素可见性"时只看填充色，
    因为网格线/坐标轴/边框都是 stroke——它们本来就该淡（见 check_P1 的注释）。
    """
    colors: set[tuple[int, int, int]] = set()
    # style="fill: #xxx" 和 style="stroke: #xxx"
    for m in re.finditer(r"style=\"([^\"]+)\"", svg):
        style = m.group(1)
        for prop in props:
            cm = re.search(prop + r":\s*([^;\"]+)", style)
            if cm:
                c = _parse_color(cm.group(1))
                if c is not None and c != (255, 255, 255):
                    colors.add(c)
    # 独立 fill="..." / stroke="..." 属性
    for m in re.finditer(r"\b(?:" + "|".join(props) + r")=\"([^\"]+)\"", svg):
        c = _parse_color(m.group(1))
        if c is not None and c != (255, 255, 255):
            colors.add(c)
    return sorted(colors)


def _extract_text_colors(svg: str) -> list[tuple[int, int, int]]:
    """文字颜色：从 `<g id="text_N">` 组取 fill；组上没写就是 SVG 默认黑色。

    为什么必须这样取：最早的实现是"全图扫 fill 再按亮度猜哪些是文字"，
    pilot 实测在饼图上把扇区填充色（#009e73、#cc79a7…）当成文字色去判 4.5:1，
    于是 P1 恒定误判（5 张图里 4 张挂，而实际文字是默认黑色、对比度 21:1）。
    亮度阈值根本区分不了"深色填充"和"深色文字"，只能按节点类型分。

    已知近似：白色文字会被跳过。白字通常画在有色扇区/色块上（matplotlib 的
    autopct 就是这样），此时"白 vs 白底"这个算法不适用，跳过比误判更合适。
    """
    out: list[tuple[int, int, int]] = []
    for m in re.finditer(r'<g id="text_\d+"[^>]*>', svg):
        # 优先取开标签上的 style，其次取组内前 400 字符（有些版本写在内层 g 上）
        cm = (re.search(r"fill:\s*([^;\"]+)", m.group(0))
              or re.search(r"fill:\s*([^;\"]+)", svg[m.start(): m.start() + 400]))
        c = _parse_color(cm.group(1)) if cm else None
        if c is None:
            c = (0, 0, 0)  # SVG 默认 fill 是黑色
        if c != (255, 255, 255):
            out.append(c)
    return out


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    """WCAG 相对亮度 (0..1)。"""
    def ch(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(rgb[0]) + 0.7152 * ch(rgb[1]) + 0.0722 * ch(rgb[2])


def _contrast_ratio(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    """WCAG 对比度 (1..21)。"""
    la, lb = _relative_luminance(a), _relative_luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


# ============================================================ 字号工具

def _extract_font_sizes(svg: str) -> list[float]:
    """从 matplotlib SVG 抽取所有文本字号 (pt)。

    matplotlib 把字号编码在 <g transform="scale(X -X)"> 里：X = fontsize / 100。
    也兼容 style="font-size: Npx" 和 font-size="N" 属性。
    """
    sizes: list[float] = []
    # matplotlib 模式：<g transform="translate(...) scale(0.1 -0.1)"> 在 text_N 组里
    for m in re.finditer(r'<g\s+id="text_\d+"[\s\S]*?scale\(([\d.]+)\s+', svg):
        sizes.append(float(m.group(1)) * 100)
    # style="font-size: Npx"
    for m in re.finditer(r"font-size:\s*([\d.]+)\s*px", svg):
        sizes.append(float(m.group(1)) * 0.75)  # px → pt (96dpi)
    # font-size="N" 属性
    for m in re.finditer(r'font-size="([\d.]+)"', svg):
        sizes.append(float(m.group(1)) * 0.75)
    return sizes


# ============================================================ 文本工具

def _extract_text_contents(svg: str) -> list[str]:
    """从 matplotlib SVG 抽取所有文本内容。

    matplotlib 把实际文字放在 <!-- text --> 注释里，紧挨着 <g id="text_N"> 标签。
    """
    texts: list[str] = []
    for m in re.finditer(r'<g\s+id="text_\d+">[\s\S]*?<!--\s*(.*?)\s*-->', svg):
        texts.append(m.group(1).strip())
    # 也抓普通 <text>...</text>（非 matplotlib 的 SVG 可能用这种）
    for m in re.finditer(r'<text[^>]*>([^<]+)</text>', svg):
        t = m.group(1).strip()
        if t:
            texts.append(t)
    return texts


_NUM_RE = re.compile(r"^[-+]?[$€£¥]?\s*[\d,]*\.?\d+\s*[%°]?(?:[eE][+-]?\d+)?[kKmM]?$")


def _extract_tick_labels(svg: str) -> list[str]:
    """抽取刻度标签文本（用于 A4 数值可读性检查）。

    2026-10-02 修：原来的正则按 `<g id="matplotlib.axis_N">...</g></g>` 截取 axis 组，
    但 matplotlib 的 axis 组里嵌套很深，第一个 `</g></g>` 出现得比刻度文本早得多，
    结果**50 张图一张都没抓到刻度**（全部判 na，A4 等于完全没在工作）。
    改成简单可靠的口径：刻度标签就是"长得像数字的文本"。
    """
    ticks: list[str] = []
    for t in _extract_text_contents(svg):
        t = t.strip()
        if t and _NUM_RE.match(t):
            ticks.append(t)
    return ticks


# ============================================================ 13 条检查

# --- L1 RULE 层 ---

def _is_encoding_color(c) -> bool:
    """是否"承载数据区分"的颜色：排除接近黑/白/灰的装饰色（网格线、背景块、边框）。

    亮度阈值与 P7（色觉安全）保持一致：颜色要用于区分类别，就不能是黑/白/灰。
    """
    return 0.05 < _relative_luminance(c) < 0.95


def check_R0_rendered(svg: str, ctx: dict) -> CheckResult:
    """R0 渲染门（critical）：图里必须真的画出了数据标记。

    之前所有规则都假设"图已经画出来了"，于是出现 D/233 这种——画面上只有两个
    飘着的数字、没有柱/线/点，却因为标题齐全、字号够大而 CPR=100%。
    这里在最前面数一遍：非白色填充的 <rect>（柱）、<circle>（散点）、
    带 fill 的 <path>（饼图扇区）。一个都没有 → 直接 fail，后面的说明质量才轮得到。
    """
    marks = 0
    # 矩形柱体
    for m in re.finditer(r"<rect\b[^>]*>", svg):
        tag = m.group(0)
        cm = re.search(r"(?:fill|style)\s*[=:]\s*[\"\']?([^;\"\' >]+)", tag)
        if cm and cm.group(1).lower() not in ("none", "white", "#ffffff", "#fff"):
            # 排除画布背景大块：width>300 视为背景而非柱体
            wm = re.search(r"width\s*[=:]\s*([\d.]+)", tag)
            if wm and float(wm.group(1)) > 300:
                continue
            marks += 1
    # 散点圆
    marks += len(re.findall(r"<circle\b[^>]*fill", svg))
    # 饼图扇区：带 fill 的 path
    for m in re.finditer(r"<path\b[^>]*>", svg):
        tag = m.group(0)
        if "fill" in tag and "none" not in tag:
            marks += 1
    if marks == 0:
        return CheckResult("R0", "Chart rendered", True, "RULE", "fail",
                           "图中未检测到任何数据标记（柱/点/扇区），渲染失败",
                           {"data_marks": 0})
    return CheckResult("R0", "Chart rendered", True, "RULE", "pass",
                       f"检测到 {marks} 个数据标记", {"data_marks": marks})


def check_P1_contrast(svg: str, ctx: dict) -> CheckResult:
    """P1 Low contrast (critical)：文本 ≥4.5:1；几何仅拦"极端看不清"(<1.5:1)。

    2026-10-15 口径定版（证据见 docs/问题清单-征求意见.md 问题7）：
    - strict（任一几何 <3:1 挂）：与人工 50 条 κ=-0.04、误伤 23/50，
      在 345 张真实图上要挂 43% —— 弃用为判分口径；
    - relaxed（只查文字）：人工一致率 96%，但盲测集 1.4:1 浅色柱 4/4 全漏；
    - 定版 relaxed15 = relaxed + 几何 <1.5:1 极端兜底：
      盲测 4/4 抓回；误伤仅 1/50（人工 pass 的 49 张里几何 <1.5:1 的
      只有 1 张 1.32:1，疑似标注噪声）。

    ctx["_contrast_mode"]="strict" 仍可复现旧口径，仅用于对比研究。
    两口径的 worst_text/worst_geom 都记录在 details 里。
    """
    mode = (ctx or {}).get("_contrast_mode", "relaxed15")
    bg = (255, 255, 255)  # matplotlib 默认白底
    # 文字按节点类型取（见 _extract_text_colors 的注释：按亮度猜会误判填充色）
    text_colors = _extract_text_colors(svg)
    text_set = set(text_colors)
    # 几何元素只算**数据元素的填充色**，不能把装饰色算进来。
    # 2026-10-02 人工抽检抓出来的严重误报：原实现把所有非文字颜色（含 stroke）
    # 都算几何元素，于是网格线(#b0b0b0)、坐标轴、图例边框这些"本来就该淡"的
    # 装饰色也被要求 ≥3:1 —— 50 张图里 40 张因此误判（人工判这 40 张都没问题，
    # κ 被拉成负值）。修法：只看 fill（数据标记），并排除接近白的画布背景。
    fills = _extract_colors(svg, props=("fill",))
    geom_colors = [c for c in fills
                   if c not in text_set and _relative_luminance(c) < 0.9]
    if not text_colors and not geom_colors:
        return CheckResult("P1", "Low contrast", True, "RULE", "na",
                           "图中没有可检测的彩色元素")
    worst_text = min((_contrast_ratio(c, bg) for c in text_colors), default=21.0)
    worst_geom = min((_contrast_ratio(c, bg) for c in geom_colors), default=21.0)
    details = {"worst_text_ratio": round(worst_text, 2),
               "worst_geom_ratio": round(worst_geom, 2),
               "mode": mode}
    if worst_text < 4.5:
        return CheckResult("P1", "Low contrast", True, "RULE", "fail",
                           f"文本对比度 {worst_text:.1f}:1 < 4.5:1", details)
    if mode == "strict":  # 旧口径，仅研究对比用
        if worst_geom < 3.0:
            return CheckResult("P1", "Low contrast", True, "RULE", "fail",
                               f"几何元素对比度 {worst_geom:.1f}:1 < 3:1", details)
        return CheckResult("P1", "Low contrast", True, "RULE", "pass",
                           f"文本 {worst_text:.1f}:1、几何 {worst_geom:.1f}:1", details)
    # 定版口径 relaxed15：几何只拦"极端看不清"
    if worst_geom < 1.5:
        return CheckResult("P1", "Low contrast", True, "RULE", "fail",
                           f"几何元素对比度 {worst_geom:.1f}:1 < 1.5:1（极端兜底）",
                           details)
    return CheckResult("P1", "Low contrast", True, "RULE", "pass",
                       f"文字 {worst_text:.1f}:1 达标；几何最低 "
                       f"{worst_geom:.1f}:1 记录不判负", details)


def check_P2_non_visual(svg: str, ctx: dict) -> CheckResult:
    """P2 Content is only visual (critical)：SVG 须有 role=img + <title> + <desc>。"""
    has_role = 'role="img"' in svg or 'role="image"' in svg
    has_title = bool(re.search(r"<title[^>]*>[^<]+</title>", svg))
    has_desc = bool(re.search(r"<desc[^>]*>[^<]+</desc>", svg))
    missing = []
    if not has_role:
        missing.append('role="img"')
    if not has_title:
        missing.append("<title>")
    if not has_desc:
        missing.append("<desc>")
    if missing:
        return CheckResult("P2", "Content is only visual", True, "RULE", "fail",
                           f"SVG 缺少: {', '.join(missing)}",
                           {"has_role": has_role, "has_title": has_title,
                            "has_desc": has_desc})
    # 2026-10-09 内容级升级：节点在，不等于内容有用。
    # (1) 占位标题（injected 兜底在没内容时会塞这些词）→ 等于还是没说
    mt = re.search(r"<title[^>]*>([^<]+)</title>", svg, re.I | re.S)
    md = re.search(r"<desc[^>]*>([^<]+)</desc>", svg, re.I | re.S)
    t = (mt.group(1).strip() if mt else "")
    d = (md.group(1).strip() if md else "")

    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()

    if _norm(t) in {"untitled chart", "untitled", "no description available",
                   "no description", "n/a"}:
        return CheckResult("P2", "Content is only visual", True, "RULE", "fail",
                           f"SVG <title> 是占位符而非真实标题: {t[:60]}",
                           {"has_role": has_role, "has_title": has_title,
                            "has_desc": has_desc, "title": t[:60]})
    # (2) title 与 desc 逐字相同 → desc 没提供任何额外信息（读屏软件念两遍同一句话）
    if t and d and _norm(t) == _norm(d):
        return CheckResult("P2", "Content is only visual", True, "RULE", "fail",
                           "<title> 与 <desc> 内容完全相同，<desc> 未提供额外说明",
                           {"has_role": has_role, "has_title": has_title,
                            "has_desc": has_desc, "title": t[:60]})
    return CheckResult("P2", "Content is only visual", True, "RULE", "pass",
                       "SVG 含 role=img + title + desc",
                       {"title": t[:60], "desc_words": len(re.findall(r"[A-Za-z]+", d))})


def check_P3_font_size(svg: str, ctx: dict) -> CheckResult:
    """P3 Small text size (critical)：所有文本 ≥12pt。

    口径变更（2026-10-09）：阈值从 9pt 提到 **12pt**，与生成提示词要求的
    "最小字号 12pt" 对齐。旧阈值 9pt 是 Chartability 的绝对底线，但提示词让
    模型做到 12pt，检查器却只按 9pt 判 → 等于纵容模型只做到 9pt、且测不出它
    "有没有真的加大字号"。现在按提示词口径判（12pt），这样 P3 才能鉴别模型
    是否真的落实了无障碍字号。代价：matplotlib 默认 10pt 的图会判 fail，这正是
    我们想暴露的差距。
    """
    sizes = _extract_font_sizes(svg)
    if not sizes:
        return CheckResult("P3", "Small text size", True, "RULE", "na",
                           "SVG 里没找到字号信息")
    min_size = min(sizes)
    if min_size < 12.0:
        return CheckResult("P3", "Small text size", True, "RULE", "fail",
                           f"最小字号 {min_size:.1f}pt < 12pt（未达到提示词要求的无障碍字号）",
                           {"min_size_pt": round(min_size, 1),
                            "all_sizes": [round(s, 1) for s in sizes[:10]]})
    return CheckResult("P3", "Small text size", True, "RULE", "pass",
                       f"最小字号 {min_size:.1f}pt ≥ 12pt",
                       {"min_size_pt": round(min_size, 1)})


def check_P6_overlap(svg: str, ctx: dict) -> CheckResult:
    """P6 Meaningful elements can't be distinguished：文字/元素不重叠。

    精确检测需要 Chrome CLI 跑 viseval 自带的 overlapScript（已在 _compat 里实现）。
    这里做轻量文本包围盒估算：如果两个 text 组的 translate 坐标太近就算可疑。
    """
    # 抽取所有 text_N 组的 translate 坐标
    coords: list[tuple[float, float, str]] = []
    for m in re.finditer(
        r'<g\s+id="text_(\d+)">[\s\S]*?'
        r'translate\(([\d.]+)\s+([\d.]+)\)', svg
    ):
        coords.append((float(m.group(2)), float(m.group(3)), m.group(1)))
    if len(coords) < 2:
        return CheckResult("P6", "Elements distinguishable", False, "RULE", "na",
                           "图中文本元素不足 2 个")
    # 简单距离估算：两个文本中心距离 < 8pt 就算可能重叠
    overlaps = []
    for i in range(len(coords)):
        for j in range(i + 1, len(coords)):
            dx = coords[i][0] - coords[j][0]
            dy = coords[i][1] - coords[j][1]
            if math.sqrt(dx * dx + dy * dy) < 8.0:
                overlaps.append((coords[i][2], coords[j][2]))
    if overlaps:
        return CheckResult("P6", "Elements distinguishable", False, "RULE", "fail",
                           f"检测到 {len(overlaps)} 对可能重叠的文本",
                           {"overlap_pairs": overlaps[:5]})
    return CheckResult("P6", "Elements distinguishable", False, "RULE", "pass",
                       "文本元素间距充足")


def check_U1_purpose(svg: str, ctx: dict) -> CheckResult:
    """U1 No explanation for purpose (critical)：须有读图说明，且不能是空话。

    2026-10-09 从"存在即过"升级为"非空且成句"：空字符串判 fail 之外，
    只有一两个词的占位（如 "Data." / "Read axes."）也判 fail——它没告诉
    读屏用户这图看什么、怎么读。真正的读图说明是 1–2 个完整句子。
    """
    note = (ctx.get("readout_note") or "").strip()
    if not note:
        return CheckResult("U1", "Purpose explanation", True, "RULE", "fail",
                           "缺少 readout_note（图表目的与读图方法说明）")
    words = re.findall(r"[A-Za-z]+", note)
    if len(words) < 3:
        return CheckResult("U1", "Purpose explanation", True, "RULE", "fail",
                           f"读图说明过短（仅 {len(words)} 个词），没有真正说明"
                           f"这张图看什么 / 怎么读: {note[:60]}",
                           {"words": len(words)})
    return CheckResult("U1", "Purpose explanation", True, "RULE", "pass",
                       f"readout_note: {note[:60]}...",
                       {"words": len(words)})


# 空话标题：只复述"这是一张图/ X 对 Y"，没说清内容或结论。
# Chartability 里 "Chart of X vs Y" 是典型反例——它没提供任何图上没有的信息。
_GENERIC_TITLES = {
    "", "figure", "plot", "chart", "graph", "untitled", "title",
    "a chart", "the chart", "the graph", "the plot",
    "y vs x", "x vs y", "chart of x vs y", "plot of x vs y",
    "graph of x vs y", "chart of y vs x",
}
# "Bar chart of A vs B" / "Plot of A versus B" 这类：只说图型和两列，不说结论
_GENERIC_TITLE_RE = re.compile(
    r"^\s*(?:the\s+)?(?:bar|line|pie|scatter|area|histogram|box|stacked)?\s*"
    r"(?:chart|graph|plot|figure)\s+of\s+.+?\s+(?:vs\.?|versus)\s+.+?\s*$",
    re.I)


def check_U2_title(svg: str, ctx: dict) -> CheckResult:
    """U2 No title/summary/caption (critical)：须有标题，且不能是空话。"""
    # 优先用扩展契约里的 title
    title = (ctx.get("title") or "").strip()
    if title:
        # 2026-10-03 修：原来 title 非空就直接 pass，下面的 generic 黑名单只在
        # fallback 分支、永远轮不到 → 实测 "Chart of X vs Y" 判 PASS。
        # 现在一律先过空话检测：标题得说清"这张图是什么、结论是什么"。
        low = title.lower().strip().rstrip("。.")
        if low in _GENERIC_TITLES or _GENERIC_TITLE_RE.match(title):
            return CheckResult("U2", "Title/summary/caption", True, "RULE", "fail",
                               f"标题是空话，没说明图表内容或结论: {title[:60]}")
        return CheckResult("U2", "Title/summary/caption", True, "RULE", "pass",
                           f"标题: {title[:60]}")
    # fallback：从 SVG 里找最大的文本当标题
    texts = _extract_text_contents(svg)
    if not texts:
        return CheckResult("U2", "Title/summary/caption", True, "RULE", "fail",
                           "SVG 里没有任何文本")
    # 排除明显的默认标题
    generic = {"y vs x", "x vs y", "", "figure", "plot"}
    candidates = [t for t in texts if t.lower().strip() not in generic]
    if candidates:
        return CheckResult("U2", "Title/summary/caption", True, "RULE", "pass",
                           f"检测到标题文本: {candidates[0][:60]}")
    return CheckResult("U2", "Title/summary/caption", True, "RULE", "fail",
                       "未找到有效标题（只有默认标签或无文本）")


# ============================================================ U3 阅读难度阈值
#
# threshold = 9 是 Chartability 的原教旨目标。但在**图表说明**这种文体上，
# Flesch-Kincaid 会系统性高估：2026-10-03 拿 35 条人工标注做反向标定
# （src/calibrate_u3.py），结论是：
#
#   阈值 9  → 与人工一致率 0.429（把人工觉得清清楚楚的说明判成"读不懂"）
#   阈值 14 → 0.943，14 再往上就是平台期（14~20 之间只差 1 个样本）
#
# 选 14 是"平台期的起点"——再严就掉出平台、再松也换不来准确率，只是白送分。
_FK_GRADE_STRICT = 9.0
_FK_GRADE_RELAXED = 14.0


def check_U3_reading_level(svg: str, ctx: dict) -> CheckResult:
    """U3 Reading level → 降级为 advisory（2026-10-15 定版）。

    calibrate_u3.py 用 35 条人工标注做格子搜索的证据：
    - 人工 34 pass / 1 fail；唯一 fail 的 FK 年级是 8.9，比 pass 组中位数
      10.3 还低 —— FK 分数与人工判断完全不相关；
    - FK 公式为连续段落标定，量"标题+两三句"方差无意义（7 词可算出 14 年级）；
    - 任何年级阈值都没有区分度：全员通过基线一致率 97.1%，与最优规则打平。

    故不再进 CPR 判分（与 U7/U9 同待遇）。FK 计算保留在 _fk_grade，
    供 calibrate_u3.py 单独报告分布；"说明缺失/电报式碎片"类问题归
    U1/U2 的 LLM 裁判。
    """
    return CheckResult("U3", "Reading level", False, "RULE", "na",
                       "降级为 advisory：FK 年级与人工判断不相关"
                       "（见 calibrate_u3.py 的格子搜索），不进 CPR 分母")

def _count_syllables(word: str) -> int:
    """简易音节计数（无需 cmudict）。"""
    word = word.lower().strip()
    if not word:
        return 0
    count = 0
    prev_v = False
    for ch in word:
        is_v = ch in "aeiouy"
        if is_v and not prev_v:
            count += 1
        prev_v = is_v
    if word.endswith("e") and count > 1:
        count -= 1
    return max(1, count)


def _fk_grade(text: str) -> float | None:
    """Flesch-Kincaid 年级水平。先试 textstat（精确），失败用启发式音节计数。"""
    try:
        import textstat
        return textstat.flesch_kincaid_grade(text)
    except Exception:
        pass
    sentences = [s.strip() for s in re.split(r"[.!?]+", text) if s.strip()]
    words = re.findall(r"\b[a-zA-Z]+\b", text)
    if not sentences or not words:
        return None
    total_syl = sum(_count_syllables(w) for w in words)
    return (0.39 * (len(words) / len(sentences))
            + 11.8 * (total_syl / len(words)) - 15.59)


def check_U7_metrics_defined(svg: str, ctx: dict) -> CheckResult:
    """U7 Metrics and variables are undefined：轴标签须含变量名/单位。

    ⚠ 2026-10-03 **降级为 advisory**（返回 ``na``，从 CPR 分子分母同时剔除）。

    原因：matplotlib SVG 把轴标签和刻度值放在**同一个** axis 组里，且顺序不固定。
    实测 `B/120/q0` 的 `matplotlib.axis_1` 组内文本为
    ``['failed parts', 'system error', 'Fault Description']`` ——
    第 1 个是 y 轴标签、第 2 个是 x 轴**刻度值**、第 3 个才是 x 轴标签。
    **结构上无法可靠区分轴标签与刻度值**。

    旧实现"只要图上有任何非数字文字就 PASS"因此等于恒 PASS
    （实跑把刻度值 ``Duplex`` 当成轴标签报 "轴标签: Duplex"），
    白占 1/13 的分母却不产生任何鉴别力。

    与其假装在查，不如明确剔出分母：CPR 分母 13 → 12，分辨力反而提升。
    真要查"单位"，需要拿到 matplotlib 的 Axes 对象（figure 级信息），
    不能在 SVG 文本层做——那属于 Phase 5 后处理可以顺手提供的上下文。
    """
    return CheckResult(
        "U7", "Metrics defined", False, "RULE", "na",
        "降级为 advisory：SVG 文本层无法区分轴标签与刻度值，不进 CPR 分母"
        "（旧实现恒 PASS，等于没查）")


def check_U9_axis_labels(svg: str, ctx: dict) -> CheckResult:
    """U9 Axis labels unclear or missing → 降级 advisory（2026-10-09）。

    为什么降级（与 U7 同一根因）：旧实现判的是"axis_1/axis_2 组里有没有
    任意 text_N"。但实测真实 SVG 里，轴组同时装着**轴标题**和**刻度文本**
    （B/120 的 axis_1 组内为 ``failed parts | system error | Fault
    Description``：前者是刻度，最后一个才是 x 轴标题）。于是：

    - 探针：轴组里只有数字刻度、没有轴标题 → 旧实现照样判 pass；
    - 任何带刻度的图都必然 pass → 这条不产生任何鉴别力，却占 1/12 的分母。

    和 U7 一样，在 SVG 文本层无法区分"轴标题"与"刻度标签"。与其假装在查，
    不如剔出分母（CPR 分母 12 → 11）。真要查轴标题，需拿 matplotlib Axes
    对象（Phase 5 后处理可提供），不能在 SVG 文本层做。
    """
    return CheckResult(
        "U9", "Axis labels", False, "RULE", "na",
        "降级为 advisory：SVG 文本层无法区分轴标题与刻度标签，不进 CPR 分母"
        "（旧实现只查轴组内有无任意文本，等于恒 pass）")


def check_C1_data_table(svg: str, ctx: dict) -> CheckResult:
    """C1 No table (critical)：须附带人类可读数据表，且数值不能和图上对不上。

    2026-10-09 从"有没有表"升级为"表在 + 数值对账"。
    抽检发现模型会编一张和图不符的表（柱图画 2/18/14，表给 1/2）——只判
    "有没有表"会把这种幻觉一并放行。判法：

    - 没有 ``data_table_csv`` → fail（不变）。
    - 有表、且 ``ctx["figure_csv"]``（从 figure 反解的真实绘图数据）可得：
      算模型表里的数字有多大比例能在图上反解数据里找到（容差 1% 或 0.5）。
      匹配率 < 50% → 判 fail（疑似整表编造 / 对错了表）。
    - 饼图（figure 表头含 ``percent``）：模型表常给计数——把表数值归一化成
      占比后对账（容差 2 个百分点，兼容四舍五入），2026-10-15 不再跳过。
    - 拿不到 figure_csv（旧批次 / 未反解）→ 退化为只查"有没有"，并在 reason 里
      写明"未对账"，不假装验过。
    """
    table = (ctx.get("data_table_csv") or "").strip()
    if not table:
        return CheckResult("C1", "Data table", True, "RULE", "fail",
                           "缺少 data_table_csv（图表对应的数据表）")

    fig_csv = (ctx.get("figure_csv") or "").strip()
    header = fig_csv.splitlines()[0].lower() if fig_csv else ""
    if fig_csv and "percent" not in header:
        real = _csv_numbers(fig_csv)
        claimed = _csv_numbers(table)
        if real and claimed:
            def _hit(v: float) -> bool:
                return any(abs(v - r) <= max(abs(r) * 0.01, 0.5) for r in real)
            misses = [v for v in claimed if not _hit(v)]
            rate = (len(claimed) - len(misses)) / len(claimed)
            details = {"table_numbers": len(claimed), "figure_numbers": len(real),
                       "match_rate": round(rate, 3),
                       "missed_values": misses[:5]}
            if rate < 1.0:
                # 2026-10-15 收紧：0.9→1.0。容差（1% 或 0.5）已覆盖四舍五入，
                # 任何真实数据值对不上都该挂——盲测集实测单行错值此前被
                # 分母摊薄逃过 0.9 阈值。
                return CheckResult("C1", "Data table", True, "RULE", "fail",
                                   f"数据表数值与图上反解数据对账，{len(misses)}/"
                                   f"{len(claimed)} 个值对不上"
                                   f"（如 {misses[0]}），疑似编造或对错了表",
                                   details)
            return CheckResult("C1", "Data table", True, "RULE", "pass",
                               f"数据表 {len(table)} 字符，数值对账匹配率 "
                               f"{rate:.0%}", details)
        return CheckResult("C1", "Data table", True, "RULE", "pass",
                           f"数据表 {len(table)} 字符（无法与图上数据逐项对账）")
    if fig_csv and "percent" in header:
        # 2026-10-15 补洞：饼图不再跳过对账。图上是百分比、模型表常给计数，
        # 把表数值归一化成占比后对账；表本身已是百分比（和≈100）则直接比。
        real = _csv_numbers(fig_csv)
        claimed = _csv_numbers(table)
        if real and claimed:
            # 双边都归一化成占比再比：模型表给计数、图上百分比可能只抽到
            # 部分扇区（和不等于 100），单边归一化会误伤（真实批次实测）
            tot_r, tot_c = sum(real), sum(claimed)
            rn = [v / tot_r * 100 for v in real] if tot_r > 0 else real
            cn = [v / tot_c * 100 for v in claimed] if tot_c > 0 else claimed
            def _pct_hit(v: float) -> bool:
                return any(abs(v - r) <= max(2.0, abs(r) * 0.05) for r in rn)
            rate = sum(1 for v in cn if _pct_hit(v)) / len(cn)
            details = {"table_numbers": len(claimed), "figure_pcts": len(real),
                       "match_rate": round(rate, 3)}
            if rate < 1.0:
                misses = [v for v in cn if not _pct_hit(v)]
                details["missed_pcts"] = [round(m, 1) for m in misses[:5]]
                return CheckResult("C1", "Data table", True, "RULE", "fail",
                                   f"饼图对账（计数归一化为占比）：{len(misses)}/"
                                   f"{len(cn)} 个值对不上，疑似数据表编造或口径错",
                                   details)
            return CheckResult("C1", "Data table", True, "RULE", "pass",
                               f"数据表 {len(table)} 字符，饼图对账匹配率 "
                               f"{rate:.0%}", details)
        return CheckResult("C1", "Data table", True, "RULE", "pass",
                           f"数据表 {len(table)} 字符（饼图口径，无可比数值）")
    return CheckResult("C1", "Data table", True, "RULE", "pass",
                       f"数据表 {len(table)} 字符（无 figure_csv，未与图上对账）")


def check_C2_query_fidelity(svg: str, ctx: dict) -> CheckResult:
    """C2 Query fidelity (critical，2026-10-15 新增)：图上数据 vs 原始查询金标。

    补 C1 的第二个洞：C1 对的是"模型写的数据表"和"图上反解数据"——模型若把
    查询本身跑错（拿到错数据），表和图会一致地错，C1 照样放行。这里拿
    visEval 数据集 vis_obj 的金标（x_data/y_data，由 build_gold_ctx.py 回填为
    ctx["gold_csv"]）再对一次账。

    - 无 gold_csv（旧批次未回填 / 探针集）→ na，不进 CPR 分母；
    - figure_csv（图上反解数据）的数值应能在金标数值里找到（容差 1% 或 0.5），
      匹配率 < 90% → fail（图把数据画错了）。
    """
    gold = (ctx.get("gold_csv") or "").strip()
    fig_csv = (ctx.get("figure_csv") or "").strip()
    if not gold or not fig_csv:
        return CheckResult("C2", "Query fidelity", True, "RULE", "na",
                           "无金标查询数据（gold_csv），未对账原始查询")
    real = _csv_numbers(fig_csv)
    gold_nums = _csv_numbers(gold)
    if not real or not gold_nums:
        return CheckResult("C2", "Query fidelity", True, "RULE", "na",
                           "图上或金标无可比数值，跳过")
    def _hit(v: float) -> bool:
        return any(abs(v - g) <= max(abs(g) * 0.01, 0.5) for g in gold_nums)
    rate = sum(1 for v in real if _hit(v)) / len(real)
    details = {"figure_numbers": len(real), "gold_numbers": len(gold_nums),
               "match_rate": round(rate, 3)}
    if rate < 0.9:
        return CheckResult("C2", "Query fidelity", True, "RULE", "fail",
                           f"图上数据与原始查询金标对账，匹配率仅 {rate:.0%}"
                           f"（{sum(1 for v in real if _hit(v))}/{len(real)}），"
                           f"查询或绘图可能拿错了数据", details)
    return CheckResult("C2", "Query fidelity", True, "RULE", "pass",
                       f"图上数据与原始查询金标对账，匹配率 {rate:.0%}", details)

# --- A3 内容级比对的工具（2026-10-03 新增）---

_ANY_NUM_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?")

# 文本中作为**标签**出现的数字：时间戳 12:00、年代 1980s/80s、编号 No. 462。
# 这些不是测量值，拿去数据表对账必然找不到 → 误报"数字对不上"
# （2026-10-16 seattle L2 "12:00 is warmest at 9.7" 的 12.0/0.0 被切成坏值、
# co2 "in the 1980s" 的 1980.0 同理；两者 L2 数字本身全对）。
_LABEL_NUM_RE = re.compile(
    r"\bNo\.?\s*\d+"          # No. 462 品种/编号
    r"|\b\d{1,2}:\d{2}\b"      # HH:MM 时间戳（08:00/12:00/18:00）
    r"|\b\d{1,4}s\b",          # 年代（1980s/2010s/80s）
    re.IGNORECASE,
)

def _strip_label_numbers(text: str) -> str:
    """剔除文本中作为标签出现的数字（时间戳/年代/编号），只留测量值。

    供 A3 的 claimed 提取与 alttext.verify_layers 的 L2 数字核验共用——
    两者对的是"文字里的数字是否能在图上数据找到"，标签数字本来就不该参与。
    """
    return _LABEL_NUM_RE.sub(" ", text or "")


def _csv_numbers(csv_text: str) -> list[float]:
    """从一段 CSV 文本里抽出数据行里的数字，用于 C1 数值对账。

    2026-10-15 修：首行含字母即视为表头并跳过——否则 "Site,1931,1932" 这类
    表头年份数字会混进数值池，稀释对账匹配率（盲测集实测 1 行错值被
    14 个数的分母摊薄到 92.9%，逃过 0.9 阈值）。
    """
    lines = (csv_text or "").strip().splitlines()
    if len(lines) > 1 and re.search(r"[A-Za-z]", lines[0]):
        lines = lines[1:]
    out: list[float] = []
    for m in _ANY_NUM_RE.finditer("\n".join(lines)):
        try:
            out.append(float(m.group().replace(",", "")))
        except ValueError:
            pass
    return out

# 方向词用词干匹配，规避时态/词形变化。刻意**排除** highest/lowest/max/min/peak
# 这类极值词——它们描述"哪个值最大"，不含"往哪走"，混进来会把趋势说反。
_UP_WORDS = ("increas", "ris", "rose", "grow", "grew", "growth", "gain",
             "upward", "uptrend", "climb")
_DOWN_WORDS = ("decreas", "declin", "drop", "fell", "falling", "reduc",
               "shrink", "downward", "downtrend", "plunge", "plummet")

# 弱证据：在讲"数据长什么样"，而不是在复述图表类型
_TREND_WORDS = ("trend", "increas", "decreas", "growth", "declin", "ris", "fell",
                "falling", "fluctuat", "vary", "varies", "steady", "stable",
                "largest", "smallest", "highest", "lowest", "maximum", "minimum",
                "majority", "dominat", "concentrat", "distribut", "correlat",
                "outlier", "spread", "gap", "clustered", "skew", "uneven",
                # 比较级和关系词也算：说"房租比公寓高"确实是在描述图上看得见的关系，
                # 只是没点名极值。这类不该判负（2026-10-03 真实图上验证过）
                "higher", "lower", "greater", "compare", "compared", "whereas",
                "above", "below", "average", "difference")

# "最高级声称"：最高级必须跟系动词连在一起，才算在指着某个类别下结论。
# 光有最高级不算 —— "the days with the highest wind speeds"（用最高级筛数据，
# 不是下结论）必须排除，否则多系列散点图会被批量误伤（2026-10-03 真实图验证）。
_EXTREME_CLAIM_RE = re.compile(
    r"\b(?:most|highest|largest|greatest|biggest|maximum|least|lowest|"
    r"smallest|minimum)\b[^.\n]{0,32}?\b(?:is|are|was|were)\b"
    r"|\b(?:is|are|was|were)\s+(?:the\s+)?"
    r"(?:most|highest|largest|greatest|biggest|maximum|least|lowest|"
    r"smallest|minimum)\b"
    r"|\b(?:has|have|had)\s+(?:the\s+)?"
    r"(?:most|highest|largest|greatest|biggest|maximum|least|lowest|"
    r"smallest|minimum)\b"
    r"|\b(?:highest|lowest|largest|smallest|most|least)\b\s+(?:at|in|on)\b"
    r"|\b(?:highest|lowest|largest|smallest|most|least|biggest)\b\s+"
    r"(?:share|portion|part|group|category|segment|proportion|majority)\b"
    r"|\bmakes? up the\s+(?:highest|lowest|largest|smallest|most|least)\b",
    re.I)


def _csv_series(csv_text: str,
                max_rows: int = 40) -> tuple[list[tuple[str, float]], int]:
    """从 CSV 抽 ``[(标签, 数值)]``，并顺带返回一行有几列（用来区分单/多序列）。

    约定：**每行最后一个数字是该行的度量值**。figure_to_csv 产出的表头只有四种
    （category,value / x,y / series,x,y / category,percent），最后一列恒为被度量的
    那个量；数字以外的单元格拼起来当这一行的标签（方便后面做"文字有没有提到它"）。
    """
    rows: list[tuple[str, float]] = []
    ncols = 0
    for line in [l.strip() for l in (csv_text or "").strip().splitlines() if l.strip()][
            : max_rows + 1]:
        cells = [c.strip() for c in line.split(",")]
        nums: list[float] = []
        labels: list[str] = []
        for c in cells:
            try:
                nums.append(float(c))
            except ValueError:
                labels.append(c)
        if not nums:
            continue  # 表头行 / 没有任何数值的行
        ncols = ncols or len(cells)
        rows.append((" ".join(labels), nums[-1]))
    return rows, ncols


def _fmt_entry(entry: tuple[str, float]) -> str:
    label, value = entry
    if abs(value - round(value)) < 1e-9:
        value = round(value)
    return f"{label}={value}" if label else str(value)


def _monotone_direction(values: list[float]) -> str | None:
    """数据是否足够单调（≥80% 的相邻差分同号）。不够单调就返回 None——不武断。"""
    if len(values) < 3:
        return None
    diffs = [values[i + 1] - values[i] for i in range(len(values) - 1)]
    if not diffs or max(values) == min(values):
        return None
    n = len(diffs)
    if sum(1 for d in diffs if d > 0) / n >= 0.8:
        return "up"
    if sum(1 for d in diffs if d < 0) / n >= 0.8:
        return "down"
    return None


def _direction_of(text: str) -> str | None:
    """文字的方向判断。同时出现涨跌两种词 → 不敢判，返回 None。"""
    words = set(re.findall(r"[a-z]+", (text or "").lower()))
    up = any(w.startswith(_UP_WORDS) for w in words)
    down = any(w.startswith(_DOWN_WORDS) for w in words)
    if up and not down:
        return "up"
    if down and not up:
        return "down"
    return None


def _trend_word(text: str) -> str:
    """返回命中的第一个趋势/关系描述词，没有则返回空串。"""
    low = (text or "").lower()
    for w in _TREND_WORDS:
        if w in low:
            return w
    return ""


def check_A3_features_described(svg: str, ctx: dict) -> CheckResult:
    """A3 Visually apparent features not described：须描述趋势/极值/异常。

    2026-10-03 升级为**内容级**检查（旧实现只看 takeaway 是否非空）。
    抽检里 30 处全是「人工 fail / 检查器 pass」的漏报：检查器看见有字就放行，
    文字其实跟画在图上的东西对不上。**"有没有描述"和"描述得对不对"是两件事**，
    而后者正是目标二（alt text）要攻的核心——这条顺带给它铺路。

    判定链（从上到下短路）：

    1. 无 takeaway → fail
    2. 有图数据 + 数据单调 + 文字有明确方向词 → **方向不一致就 fail**
       （最硬的证据：文字说在涨、图其实在跌）
    3. 文字提到图上极值的**类别名或数值** → pass（戳到了主要特征）
    4. 文字含趋势/分布/关系词 → pass（弱证据，details 标 via=trend_word）
    5. 都没有 → fail（只是把图表类型复述了一遍）

    数据源优先级：``ctx["figure_csv"]``（从 figure 反解，可靠）>
    ``ctx["data_table_csv"]``（模型自报，**可能是编的**）> 都没有（退化为只查非空）。
    """
    takeaway = (ctx.get("takeaway") or "").strip()
    if not takeaway:
        return CheckResult("A3", "Features described", False, "RULE", "fail",
                           "缺少 takeaway（趋势/极值/异常的文字描述）",
                           {"source": "none", "verified": False})

    fig_csv = (ctx.get("figure_csv") or "").strip()
    own_csv = (ctx.get("data_table_csv") or "").strip()
    source = "figure_csv" if fig_csv else ("data_table_csv" if own_csv else "none")
    rows, ncols = _csv_series(fig_csv or own_csv)
    details: dict = {"source": source, "n_rows": len(rows)}

    # (2) 方向一致性：只在「单序列 + 数据够单调 + 文字说得够明确」时才敢判
    if rows and ncols <= 2:
        values = [v for _, v in rows]
        data_dir = _monotone_direction(values)
        text_dir = _direction_of(takeaway)
        if data_dir and text_dir:
            details.update({"data_direction": data_dir, "text_direction": text_dir})
            if data_dir != text_dir:
                return CheckResult(
                    "A3", "Features described", False, "RULE", "fail",
                    f"文字说的是{'上升' if text_dir == 'up' else '下降'}，"
                    f"图上的数据却在{'上升' if data_dir == 'up' else '下降'}",
                    {**details, "verified": True})

    if rows:
        claimed: list[float] = []
        for m in _ANY_NUM_RE.finditer(_strip_label_numbers(takeaway)):
            try:
                claimed.append(float(m.group().replace(",", "")))
            except ValueError:
                pass
        hay = takeaway.lower()

        def _mentioned(entry: tuple[str, float]) -> bool:
            """这一行有没有被文字提到（按类别名或数值，任一即可）。"""
            # 词边界匹配。2026-10-03 修：原来是 `w.lower() in hay` 子串匹配，
            # 类别 `system error` 的 "error" 会被 takeaway 里的 **"errors"** 命中
            # （"errors" 含子串 "error"）→ 完全没提这个类别却判 PASS。
            for w in re.findall(r"[A-Za-z]{1,}", entry[0]):
                if re.search(r"\b" + re.escape(w.lower()) + r"\b", hay):
                    return True
            tol = max(abs(entry[1]) * 0.02, 0.5)
            return any(abs(v - entry[1]) <= tol for v in claimed)

        top = max(rows, key=lambda kv: kv[1])
        bottom = min(rows, key=lambda kv: kv[1])
        mentioned = [e for e in rows if _mentioned(e)]
        details.update({"top": _fmt_entry(top), "bottom": _fmt_entry(bottom),
                        "mentioned_any": bool(mentioned)})

        # (3a) 最高级下结论、却没点到图上任何真实类别/数值 → 编造（2026-10-09 堵漏 ①）
        #   旧漏洞：弱证据分支只要命中 "highest" 就 pass，于是
        #   "The highest is blue whale at 999."（图上既无此类别、也无此值）照样满分。
        #   现在：只要动了最高级（most/highest...is），就必须戳中真实的 top 或 bottom。
        if _EXTREME_CLAIM_RE.search(takeaway) and not mentioned:
            return CheckResult(
                "A3", "Features described", False, "RULE", "fail",
                f"文字用最高级下结论，但图上既未提到最大的 {_fmt_entry(top)}、"
                f"也未提到最小的 {_fmt_entry(bottom)}（疑似编造）",
                {**details, "verified": True, "fabricated_claim": True})

        # (3) 最高级声称校验：说"X 是最…的"，就必须提到图上真正的那个极值。
        #     这条最值钱 —— 它抓的是**文字和图在时间维上的硬冲突**（比方图上只有
        #     二月和三月，文字却说"最多的是十二月"），也正是目标二要防的幻觉。
        #     两个条件同时满足才敢判，避免把 "the days with the highest wind
        #     speeds"（在用最高级筛数据，不是在下结论）误伤成失败。
        if _EXTREME_CLAIM_RE.search(takeaway) and mentioned and not _mentioned(top):
            return CheckResult(
                "A3", "Features described", False, "RULE", "fail",
                f"文字点名了某些类别并用了最高级，但图上最大的其实是 "
                f"{_fmt_entry(top)}", {**details, "verified": True})

        # (4) 提到了图上任何一个具体的东西 → 在说这张图，放行
        if mentioned:
            entry = max(mentioned, key=lambda kv: kv[1])
            return CheckResult("A3", "Features described", False, "RULE", "pass",
                               f"文字提到了图上的 {_fmt_entry(entry)}",
                               {**details, "verified": True})

    # (5) 弱证据：至少在讲数据的样子（趋势 / 比较 / 分布）
    tw = _trend_word(takeaway)
    # 但如果有图数据，文字却瞎报涨跌方向、又没提到任何真实类别/数值 → 不兜底放行
    if rows and _direction_of(takeaway) and not mentioned:
        return CheckResult(
            "A3", "Features described", False, "RULE", "fail",
            f"文字断言了涨跌方向，但没提到图上任何类别或数值，无法核对",
            {**details, "verified": False})
    if tw:
        return CheckResult("A3", "Features described", False, "RULE", "pass",
                           f"含趋势/关系描述词「{tw}」（未与图上数值逐项比对）",
                           {**details, "via": "trend_word", "verified": False})

    # (5) 只是在复述图表类型
    if rows:
        reason = ("文字没有提到图上的任何类别或数值"
                  f"（图上最大 {_fmt_entry(max(rows, key=lambda kv: kv[1]))}）")
    else:
        reason = "takeaway 没有描述任何趋势/极值/异常，且无图数据可比对"
    return CheckResult("A3", "Features described", False, "RULE", "fail",
                       reason, {**details, "verified": bool(rows)})


def check_A4_human_readable(svg: str, ctx: dict) -> CheckResult:
    """A4 Data in text is not human-readable：刻度标签须人类可读。"""
    ticks = _extract_tick_labels(svg)
    if not ticks:
        return CheckResult("A4", "Human-readable data", False, "RULE", "na",
                           "未找到刻度标签")
    bad = []
    for t in ticks:
        # 科学计数法
        if "e" in t.lower() and re.search(r'\d+e[+-]?\d', t, re.I):
            bad.append(t)
        # 过长小数 (>4 位)
        elif re.search(r'\.\d{5,}', t):
            bad.append(t)
        # 过大未缩写的整数 (>=7 位)
        elif re.match(r'-?\d{7,}$', t):
            bad.append(t)
    if bad:
        return CheckResult("A4", "Human-readable data", False, "RULE", "fail",
                           f"不可读的刻度: {bad[:3]}",
                           {"bad_labels": bad[:5], "total_ticks": len(ticks)})
    return CheckResult("A4", "Human-readable data", False, "RULE", "pass",
                       f"所有 {len(ticks)} 个刻度标签均可读")


# --- L2 CVD 层 ---

# Okabe-Ito 色盲安全调色板（直接命中就 PASS，不用跑仿真）
_OKABE_ITO_HEX = {
    "#e69f00", "#56b4e9", "#009e73", "#f0e442",
    "#0072b2", "#d55e00", "#cc79a7", "#000000",
}
# viridis 系也安全（matplotlib 的 tab10 不安全 —— #2ca02c 和 #d62728 是著名的红绿坑）

# Machado 2009 仿真矩阵（severity=1.0）
_CVD_MATRICES = {
    "protanopia":   [0.152286, 1.052583, -0.204868,
                     0.114503, 0.786281,  0.099216,
                    -0.003882, -0.048116, 1.051998],
    "deuteranopia": [0.367322, 0.860646, -0.227968,
                     0.280085, 0.672501,  0.047413,
                    -0.011820, 0.042940,  0.968881],
    "tritanopia":   [1.255528, -0.076749, -0.178779,
                    -0.078411, 0.930809,  0.147602,
                     0.004733, 0.691367,  0.303900],
}

def _simulate_cvd(rgb: tuple[int, int, int], cvd_type: str) -> tuple[float, float, float]:
    """跑色盲仿真，返回模拟后的 RGB（可能超出 0-255，要 clamp）。"""
    m = _CVD_MATRICES[cvd_type]
    r, g, b = rgb
    sr = m[0]*r + m[1]*g + m[2]*b
    sg = m[3]*r + m[4]*g + m[5]*b
    sb = m[6]*r + m[7]*g + m[8]*b
    return (max(0, min(255, sr)), max(0, min(255, sg)), max(0, min(255, sb)))


def _delta_e2000(rgb1: tuple, rgb2: tuple) -> float:
    """感知色差（CAM02-UCS 欧氏距离，近似 CIEDE2000）。

    ⚠ 2026-10-03 弃用 colorspacious：该库 2018 年后停更，与 numpy 2.x 不兼容，
    **import 它会导致进程段错误**（不是抛异常，try/except 兜不住，
    整个评测进程直接死）。本机实测 numpy 2.4.6 + colorspacious → segfault。

    现在一律走纯 Python 的 CIELAB 欧氏距离（ΔE*_ab），无原生依赖，
    量级与 CAM02-UCS / CIEDE2000 同档（都是"感知色差"，0–100），
    阈值 6.0 沿用它在这个量级上的经验含义。
    """
    return _delta_e_lab(rgb1, rgb2)


def _delta_e_lab(rgb1: tuple, rgb2: tuple) -> float:
    """降级色差：RGB → Lab → 欧氏距离。"""
    def rgb_to_lab(rgb):
        # RGB → XYZ → Lab（sRGB, D65）
        r, g, b = [c / 255.0 for c in rgb]
        r = ((r + 0.055) / 1.055) ** 2.4 if r > 0.04045 else r / 12.92
        g = ((g + 0.055) / 1.055) ** 2.4 if g > 0.04045 else g / 12.92
        b = ((b + 0.055) / 1.055) ** 2.4 if b > 0.04045 else b / 12.92
        x = (r * 0.4124 + g * 0.3576 + b * 0.1805) * 100
        y = (r * 0.2126 + g * 0.7152 + b * 0.0722) * 100
        z = (r * 0.0193 + g * 0.1192 + b * 0.9505) * 100
        x, y, z = x / 95.047, y / 100.0, z / 108.883
        fx = x ** (1/3) if x > 0.008856 else 7.787 * x + 16/116
        fy = y ** (1/3) if y > 0.008856 else 7.787 * y + 16/116
        fz = z ** (1/3) if z > 0.008856 else 7.787 * z + 16/116
        return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)
    l1, a1, b1 = rgb_to_lab(rgb1)
    l2, a2, b2 = rgb_to_lab(rgb2)
    return math.sqrt((l1-l2)**2 + (a1-a2)**2 + (b1-b2)**2)


def check_P7_cvd_safe(svg: str, ctx: dict) -> CheckResult:
    """P7 Not CVD-friendly：色盲仿真后编码色两两 ΔE2000 ≥ 阈值。"""
    colors = _extract_colors(svg)
    # 过滤掉黑/白/灰（它们不是编码色）
    encode_colors = [c for c in colors if _is_encoding_color(c)]
    if len(encode_colors) <= 1:
        return CheckResult("P7", "CVD-friendly", False, "CVD", "na",
                           "图中只有 0-1 种编码色，颜色不承载区分信息")
    # 白名单：Okabe-Ito
    color_hexes = {"#{:02x}{:02x}{:02x}".format(*c) for c in encode_colors}
    if color_hexes <= _OKABE_ITO_HEX:
        return CheckResult("P7", "CVD-friendly", False, "CVD", "pass",
                           "使用 Okabe-Ito 色盲安全配色")
    # matplotlib tab10 默认配色里的红绿对——Chartability 明确点名
    _BAD_PAIRS = {frozenset({(214, 39, 40), (44, 160, 44)})}  # #d62728 vs #2ca02c
    encode_set = set(encode_colors)
    for bad in _BAD_PAIRS:
        if bad <= encode_set:
            return CheckResult("P7", "CVD-friendly", False, "CVD", "fail",
                               "matplotlib 默认红绿配色在色盲下不可区分",
                               {"bad_pair": [str(c) for c in bad]})

    # 跑三种色盲仿真，找最差的色对
    threshold = 6.0  # CAM02-UCS 距离 < 6 = 难以区分
    worst = {"type": "", "pair": None, "delta_e": 999.0}
    for cvd_type in ("deuteranopia", "protanopia", "tritanopia"):
        sim_colors = [_simulate_cvd(c, cvd_type) for c in encode_colors]
        for i in range(len(sim_colors)):
            for j in range(i + 1, len(sim_colors)):
                de = _delta_e2000(sim_colors[i], sim_colors[j])
                if de < worst["delta_e"]:
                    worst = {"type": cvd_type, "pair": (encode_colors[i], encode_colors[j]),
                             "delta_e": de}
    if worst["delta_e"] < threshold:
        return CheckResult("P7", "CVD-friendly", False, "CVD", "fail",
                           f"{worst['type']} 下色差 ΔE={worst['delta_e']:.1f} < {threshold}",
                           {"worst_cvd": worst["type"],
                            "worst_delta_e": round(worst["delta_e"], 1),
                            "worst_pair": [str(c) for c in worst["pair"]]})
    return CheckResult("P7", "CVD-friendly", False, "CVD", "pass",
                       f"三种色盲下最小 ΔE={worst['delta_e']:.1f} ≥ {threshold}",
                       {"worst_cvd": worst["type"],
                        "worst_delta_e": round(worst["delta_e"], 1)})


# ============================================================ 编排器

ALL_CHECKS: list[Callable[[str, dict], CheckResult]] = [
    check_R0_rendered,
    check_P1_contrast,
    check_P2_non_visual,
    check_P3_font_size,
    check_P6_overlap,
    check_P7_cvd_safe,
    check_U1_purpose,
    check_U2_title,
    check_U3_reading_level,
    check_U7_metrics_defined,
    check_U9_axis_labels,
    check_C1_data_table,
    check_C2_query_fidelity,
    check_A3_features_described,
    check_A4_human_readable,
]

# 7 条 critical 的 ID（用于 cpr_critical 子集）
CRITICAL_IDS = {"R0", "P1", "P2", "P3", "U1", "U2", "C1", "C2"}  # U3 降级 advisory；C2 新增


def run_chartability(svg: str, ctx: dict | None = None) -> list[CheckResult]:
    """跑全部 MAIN 检查。

    svg: matplotlib 生成的 SVG 文本
    ctx: 扩展输出契约字段 {"title": str, "readout_note": str,
                          "takeaway": str, "data_table_csv": str}
    """
    ctx = ctx or {}
    results: list[CheckResult] = []
    for fn in ALL_CHECKS:
        try:
            results.append(fn(svg, ctx))
        except Exception as exc:
            # 检查器自身不能挂掉整条管线
            results.append(CheckResult(
                id=fn.__name__.split("_")[1],
                name=fn.__doc__.split("：")[0] if fn.__doc__ else fn.__name__,
                critical=False, layer="RULE", status="skip",
                reason=f"检查器内部错误: {exc}",
            ))

    # LLM 质量层总开关（2026-10-16 接入主流程时加）：
    # A11Y_LLM_JUDGE=0 强制回到纯规则模式（G0 门与五项打分都不跑）。
    llm_on = _llm_layer_on(ctx)

    # G0：VLM 前置可读门（仅 LLM 层开启时）。门不过则语义项不评估。
    readable = None
    if llm_on:
        try:
            from .vlm_layer import vlm_legibility_gate, SEMANTIC_IDS
            gate, readable = vlm_legibility_gate(svg, ctx, ctx.get("_svg_path"))
            if gate is not None:
                results.insert(0, gate)
                if readable is False:
                    results = [gate] + [
                        (CheckResult(r.id, r.name, r.critical, r.layer, "na",
                                     "G0 判定图表不可读，语义项不予评估(not_assessable)",
                                     r.details) if r.id in SEMANTIC_IDS else r)
                        for r in results if r is not gate]
        except Exception as exc:
            results.insert(0, CheckResult("G0", "VLM legibility gate", True,
                                          "VLM", "skip", f"G0 接线异常: {exc!r}"[:120]))

    # LLM 语义质量层（五项 0/1/2 打分，只追加不替换规则结果）：
    # G0 判定无数据标记（readable=False）时图上没东西可描述，五项全跳过不调 LLM；
    # readable=None（G0 自身 skip，如截图失败）时文本四项仍可评，各项自行兜底。
    if llm_on and readable is not False:
        try:
            from .vlm_layer import run_vlm_judge
            results.extend(run_vlm_judge(svg, ctx, ctx.get("_svg_path")))
        except Exception as exc:
            results.append(CheckResult("VLM", "VLM judge", False, "VLM", "skip",
                                       f"VLM 层接线异常: {exc!r}"[:120]))
    return results


def compute_cpr(results: list[CheckResult]) -> CPR:
    """从 13 条判定结果算 CPR。

    layer=="VLM" 的 LLM 质量项（G0 门 + U1/U2/A3/L3/L4 五项打分）整层排除：
    规则 CPR 保持 100% 可复现、与历史批次同口径；LLM 分走 compute_llm_cpr
    单独算（需求 8：LLM 分只做 advisory，不进 critical 集）。
    """
    results = [r for r in results if getattr(r, "layer", "") != "VLM"]
    applicable = [r for r in results if r.status in ("pass", "fail")]
    passed = [r for r in applicable if r.status == "pass"]
    crit_applicable = [r for r in applicable if r.id in CRITICAL_IDS]
    crit_passed = [r for r in crit_applicable if r.status == "pass"]
    return CPR(
        cpr_all=len(passed) / len(applicable) if applicable else None,
        cpr_critical=len(crit_passed) / len(crit_applicable) if crit_applicable else None,
        total=len(results),
        applicable=len(applicable),
        passed=len(passed),
        failed=len(applicable) - len(passed),
        na=sum(1 for r in results if r.status == "na"),
        skip=sum(1 for r in results if r.status == "skip"),
        failures={r.id: r.reason for r in applicable if r.status == "fail"},
    )
def _llm_layer_on(ctx: dict | None) -> bool:
    """LLM 质量层总开关：ctx["_vlm"]=True 且环境变量 A11Y_LLM_JUDGE != 0。"""
    return bool((ctx or {}).get("_vlm")) and os.environ.get("A11Y_LLM_JUDGE", "1") != "0"

LLM_JUDGE_IDS = ("U1", "U2", "A3", "L3", "L4")

def compute_llm_cpr(results: list[CheckResult]) -> dict:
    """LLM 质量分（与规则 CPR 同口径：>=1 为 pass，skip/na 剔除分母）。

    只认 layer=="VLM" 且 id 在 LLM_JUDGE_IDS 的项。返回：
    {"u1": 0/1/2/null, ..., "pass_count": int, "applicable": int,
     "llm_cpr": float/null, "llm_skipped": [id...]}
    applicable==0（如无 key 全 skip）→ llm_cpr 为 null，但结构始终完整——
    下游 report 永远能拿到 cpr_llm 字段（需求 6.2：失败不炸）。
    """
    llm = [r for r in results
           if getattr(r, "layer", "") == "VLM" and r.id in LLM_JUDGE_IDS]
    scores: dict = {}
    applicable = passed = 0
    skipped: list[str] = []
    for r in llm:
        key = r.id.lower()
        s = (r.details or {}).get("vlm_score")
        scores[key] = s if isinstance(s, int) else None
        if r.status in ("pass", "fail"):
            applicable += 1
            passed += 1 if r.status == "pass" else 0
        elif r.status == "skip":
            skipped.append(key)
    return {**scores, "pass_count": passed, "applicable": applicable,
            "llm_cpr": (passed / applicable) if applicable else None,
            "llm_skipped": skipped}
