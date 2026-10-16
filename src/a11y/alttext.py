"""目标二·自然语言描述：按 Lundgard & Satyanarayan 四层模型（arXiv:2110.04406）
为生成的可视化产出分层 alt-text。

四层与可信度（论文口径，报告时要分层分开讲）：
- L1 construction：图型、坐标轴、单位、数据来源形态 —— 可从数据**确定性验证**
- L2 statistics：极值、趋势、对比 —— 可从数据**确定性验证**（数字必须在表里）
- L3 phenomena：聚类/离群/分布形态等感知现象 —— 模型生成，中等可信
- L4 context：与用户领域问题的关联解读 —— 模型生成，低可信，须标明推测性

设计要点：
- 描述生成是**独立一步**，不塞进代码生成模型（关注点分离：写图和写话两个任务）；
- L1/L2 生成后跑确定性核验（数字/图型对不上就重写一次）；
- 输出单条可读文本（screen reader 顺序读四层）+ 分层 JSON（供论文/标注用）；
- 最终写进 SVG <desc>（复用 contract.inject_svg_metadata）。
"""
from __future__ import annotations

import csv as _csv
import io as _io
import json
import re

from .checker import _strip_label_numbers
from .contract import inject_svg_metadata
from .vlm_layer import _TX_MODEL, _call, _client

LAYER_FIELDS = [
    ("l1_construction", "L1 construction 图表构造"),
    ("l2_statistics", "L2 statistics 统计关系"),
    ("l3_patterns", "L3 phenomena 感知现象"),
    ("l4_context", "L4 context 领域语境"),
]

ALT_RUBRIC = """You write alternative text for a data chart, following the four-layer model \
(Lundgard & Satyanarayan 2021). You get the chart title and the exact data table (CSV) \
that the chart plots. Write FOUR layers, each 1-2 short plain sentences:

- l1_construction: chart type, what is on x/y axis, units, how many categories/points.
- l2_statistics: name concrete extreme values and trends with the ACTUAL numbers from \
the table (e.g. "X is highest at 30.5"). Never invent a number that is not in the table.
- l3_patterns: notable patterns a sighted reader perceives (clusters, outliers, gaps, \
shape of the distribution). Only claim patterns the table supports.
- l4_context: one sentence linking the chart back to the user's question, marked as \
interpretation. Start it with "This suggests".

Rules: plain short sentences (<= 18 words), no "respectively/predominantly/distribution" \
jargon, no markdown. Return strict JSON:
{"l1_construction":"...","l2_statistics":"...","l3_patterns":"...","l4_context":"..."}"""


def _table_numbers(csv_text: str) -> list[float]:
    # 逐单元格解析。旧实现用数字正则扫全文本，会把 CSV 相邻字段连成一个数
    # （"1931,65.7667" -> 462193165.7667），导致 L2 核验大面积误报
    # （2026-10-15 实测 demo_barley l2_verified=False）。
    out = []
    lines = (csv_text or "").strip().splitlines()
    if len(lines) > 1 and re.search(r"[A-Za-z]", lines[0]):
        lines = lines[1:]
    for line in lines:
        for cell in line.split(","):
            cell = cell.strip()
            if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", cell):
                out.append(float(cell))
    return out


def _chat(prompt: str, cache_key: str) -> str:
    # GLM-5.3-Flash 服务端"始终思考"（不支持 disabled），reasoning 吃
    # max_tokens：700 时四层内容会被截断。low 挡 + 4000 实测完整返回。
    return _call(_client(), _TX_MODEL, [{"role": "user", "content": prompt}],
                 tokens=16000, cache_key=cache_key,
                 extra_body={"thinking": {"type": "low"}})


def _is_numeric_cell(s: str) -> bool:
    try:
        float(str(s).replace(",", "").strip())
        return True
    except (TypeError, ValueError):
        return False


def _fmt(v: float) -> str:
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.3g}"


def _points_from_table(header: list, rows: list):
    """把两种表形态统一解析成 (标签, 值) 点序列，并返回系列名集合。

    - figure 反解的**长表** series,x,y（多系列线/散点）：值列是 y，
      标签拼"系列名 + x"，x 列是坐标不是度量，绝不能当成第二个系列；
      （2026-10-15 踩坑：把年份 x 当数值列，极值报成 "x Nigeria at 2021"）
    - 长表 x,y / category,value / category,percent（单系列）：最后一列是值。
    - VLM 提取的**宽表** Year,Nigeria,...,India：每个数值列是一条系列，
      标签拼"系列名 + 行名"。
    返回 (points, series_names)。
    """
    low = [str(h).strip().lower() for h in header]
    points, series_names = [], []
    if low == ["series", "x", "y"]:
        for r in rows:
            if len(r) >= 3 and _is_numeric_cell(r[2]):
                points.append((f"{r[0]} {r[1]}".strip(),
                               float(r[2].replace(",", ""))))
                if r[0] not in series_names:
                    series_names.append(r[0])
        return points, series_names
    if len(header) == 2 and not _is_numeric_cell(header[0]):
        # category,value / x,y（首列表头是文字）：首列标签、末列值
        for r in rows:
            if len(r) >= 2 and _is_numeric_cell(r[1]):
                points.append((r[0].strip(), float(r[1].replace(",", ""))))
        return points, []
    # 宽表：首列若为行标签（非数字），其余数字列各为一条系列
    label_col = 0 if (header and not _is_numeric_cell(header[0])) else None
    value_cols = [j for j in range(len(header))
                  if j != label_col
                  and any(_is_numeric_cell(r[j]) for r in rows if j < len(r))]
    multi = len(value_cols) > 1
    for r in rows:
        row_name = r[label_col].strip() if label_col is not None and label_col < len(r) else ""
        for j in value_cols:
            if j < len(r) and _is_numeric_cell(r[j]):
                v = float(r[j].replace(",", ""))
                parts = ([header[j], row_name] if multi else [row_name or header[j]])
                points.append((" ".join(p for p in parts if p).strip(), v))
                if multi and header[j] not in series_names:
                    series_names.append(header[j])
    return points, series_names


def _template_takeaway(points: list) -> str:
    """从 (标签, 值) 点序列写全局极值句。"""
    if len(points) < 2:
        return ""
    tname, tval = max(points, key=lambda kv: kv[1])
    bname, bval = min(points, key=lambda kv: kv[1])
    return (f"The highest value is {tname} at {_fmt(tval)}, "
            f"and the lowest is {bname} at {_fmt(bval)}.")


def deterministic_alt_text(title: str, table_csv: str,
                           chart_type: str = "", question: str = "") -> dict:
    """LLM 不可用（无 key / 超时 / 空响应）时的确定性兜底。

    只生产**本就能从数据确定**的 L1/L2：L1 说图型/轴/点数，L2 写全局极值
    （数字全部来自表，L2 核验必过）。L3（感知现象）/L4（领域解读）是模型
    解读层，无模型时**宁可留空也不编造**——与四层模型的可信度分层一致。
    """
    try:
        rows = [r for r in _csv.reader(_io.StringIO((table_csv or "").strip()))
                if r and any(c.strip() for c in r)]
    except Exception:
        rows = []
    l1, l2 = "", ""
    if rows:
        header, data = rows[0], rows[1:]
        ct = (chart_type or "").lower().strip()
        noun = f"{ct} chart" if ct and ct != "chart" else "chart"
        points, series = _points_from_table(header, data)
        l2 = _template_takeaway(points)
        if series:  # 多系列（长表 series,x,y 或宽表）
            n = len(points) // len(series)
            l1 = (f"{noun} with about {n} data points per series "
                  f"across {len(series)} series ({', '.join(series)}).")
        else:
            if len(header) >= 2:
                l1 = (f"{noun} plotting {header[-1]} against {header[0]}, "
                      f"with {len(data)} data points.")
            else:
                l1 = f"{noun} with {len(data)} data points."
    return {"l1_construction": l1, "l2_statistics": l2,
            "l3_patterns": "", "l4_context": "",
            "title": title, "chart_type": chart_type,
            "fallback": "deterministic_l1_l2"}


def generate_alt_text(title: str, table_csv: str, question: str = "",
                      chart_type: str = "") -> dict:
    """生成四层描述。question 是用户的原始问题（喂 L4），可为空。

    LLM 返回空/无法解析时降级到 deterministic_alt_text（L1/L2 确定性兜底），
    绝不返回四层全空——旧实现遇到服务端失败会静默产出空描述，
    读屏用户拿到的只是一张没声音的图（2026-10-15 图片模式 demo 实测）。
    """
    payload = (f"Chart title: {title or '(none)'}\n"
               f"User question: {question or '(not provided)'}\n"
               f"Data table (exact values plotted):\n{table_csv}\n"
               f"Respond with the JSON only.")
    try:
        raw = _chat(ALT_RUBRIC + "\n\n" + payload,
                    cache_key="alt|" + ALT_RUBRIC + payload)
    except Exception:
        # 描述服务是附加层：认证失败/超时/网络异常都不该让整条生成链崩掉，
        # 降级到确定性 L1/L2 兜底。
        raw = ""
    m = re.search(r"\{[\s\S]*\}", raw)
    try:
        parsed = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        parsed = {}
    alt = {k: (parsed.get(k) or "").strip()
           for k in ("l1_construction", "l2_statistics", "l3_patterns", "l4_context")}
    if not any(alt.values()):
        return deterministic_alt_text(title, table_csv, chart_type, question)
    alt["title"] = title
    alt["chart_type"] = chart_type
    return alt


def verify_layers(alt: dict, table_csv: str) -> dict:
    """L1/L2 确定性核验（L3/L4 是解读层，只标注不核验）。

    - l2 里的数字必须能在数据表里找到（容差 1% 或 0.5），否则 l2_verified=False；
    - l1 必须提到一种图型词，否则 l1_verified=False。
    """
    nums = _table_numbers(table_csv)

    def hit(v: float) -> bool:
        return any(abs(v - g) <= max(abs(g) * 0.01, 0.5) for g in nums)

    # "No. 462" 这类品种/编号名会被数字正则误提取（barley 品种 No. 457/462），
    # 先剔除再提取（2026-10-15 实测误报 l2_verified=False）
    # 先剔除标签数字（No. 462 编号 / 12:00 时间戳 / 1980s 年代）再提取，
    # 否则被当测量值对账而误报（2026-10-15 barley No.457、2026-10-16
    # seattle "12:00 is warmest" 的 12.0/0.0、co2 "in the 1980s" 的 1980.0）
    l2_text = _strip_label_numbers(alt.get("l2_statistics", ""))
    l2_nums = [float(x.replace(",", "")) for x in
               re.findall(r"[-+]?\d[\d,]*(?:\.\d+)?", l2_text)]
    l2_bad = [v for v in l2_nums if not hit(v)]
    chart_words = ("bar", "pie", "line", "scatter", "histogram", "stacked",
                   "dot plot", "chart", "plot")
    l1 = alt.get("l1_construction", "").lower()
    return {
        "l2_verified": bool(l2_nums) and not l2_bad,
        "l2_bad_values": l2_bad[:5],
        "l1_verified": any(w in l1 for w in chart_words),
        # L3/L4 是解读层，规则层不验；下游 run_goal2._finish 拿到 LLM 分数后
        # 会回写成 llm_scored(l3=..,l4=..) 或 llm_skipped。
        "l3_l4": "pending_llm_judge",
    }


def combine(alt: dict) -> str:
    """拼成单条 screen-reader 可读文本，四层按序、带层标。"""
    parts = [f"[{label}] {alt.get(key, '').strip()}"
             for key, label in LAYER_FIELDS if alt.get(key, '').strip()]
    return " ".join(parts)


def inject_alt(svg: str, alt: dict, table_csv: str = "") -> str:
    """把分层描述写进 SVG <desc>（title 保持不变）。"""
    combined = combine(alt) or alt.get("l2_statistics", "")
    return inject_svg_metadata(svg, title=alt.get("title", ""), desc=combined)
