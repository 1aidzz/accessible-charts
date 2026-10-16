"""从 matplotlib figure 反解「图里到底画了哪些数」。

两个用途
--------
1. injected 组兜底：模型没给数据表时，用它生成 data_table_csv（见 contract.build_injected）。
2. 幻觉检测（advisory）：把模型自报的 CSV 与图里真实数据比对，算 table_match_rate。
   这是目标二（alt text）的前置能力——L2 层描述里的数字必须能对着数据校验，
   否则模型写"最高值是 X"我们无从判断真假。

为什么不用 eval 后的 df：df 是**输入**数据，图里画的是**聚合后**的数据；
Chartability 的 C1 要的是"图上这个点是多少"，只有从 figure 反解才准。
"""

from __future__ import annotations

import io
import math


def _fmt(v) -> str:
    """数字友好格式化：整数不带 .0，浮点保留 3 位有效小数。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if not math.isfinite(f):
        return str(v)
    if abs(f - round(f)) < 1e-9:
        return str(int(round(f)))
    return f"{f:.3g}"


def _tick_labels(ax) -> list[str]:
    """x 轴刻度文字（bar 图的类别名）。"""
    labels = []
    try:
        pos = list(ax.get_xticks())
        texts = [t.get_text() for t in ax.get_xticklabels()]
        for p, t in zip(pos, texts):
            labels.append((p, (t or "").strip()))
    except Exception:
        return []
    return labels


def _bars(ax) -> list[tuple]:
    """柱/条：返回 [(类别名, 数值)]。"""
    out = []
    ticks = _tick_labels(ax)
    try:
        patches = [p for p in ax.patches if hasattr(p, "get_height") and hasattr(p, "get_width")]
    except Exception:
        return out
    for p in patches:
        try:
            w, h = p.get_width(), p.get_height()
        except Exception:
            continue
        # 水平条形图：宽度才是数值
        value = h if h >= w else w
        coord = p.get_x() + w / 2 if h >= w else p.get_y() + h / 2
        name = ""
        best = None
        for pos, t in ticks:
            d = abs(pos - coord)
            if best is None or d < best:
                best, name = d, t
        out.append((name or _fmt(coord), value))
    return out


def _lines(ax) -> list[tuple]:
    """折线/散点线：返回 [(系列名, [(x, y), ...])]。"""
    out = []
    try:
        for ln in ax.lines:
            xd = ln.get_xdata(orig=True)
            yd = ln.get_ydata(orig=True)
            if len(xd) == 0 or len(yd) == 0:
                continue
            label = ln.get_label() or ""
            if label.startswith("_child"):  # matplotlib 自动命名的未标注线条
                label = ""
            out.append((label, list(zip(list(xd), list(yd)))))
    except Exception:
        pass
    return out


def _scatter(ax) -> list[tuple]:
    out = []
    try:
        for c in ax.collections:
            offs = c.get_offsets()
            if offs is None or len(offs) == 0:
                continue
            label = c.get_label() or ""
            if label.startswith("_child"):
                label = ""
            pts = [(float(p[0]), float(p[1])) for p in offs]
            out.append((label, pts))
    except Exception:
        pass
    return out


def _pie(ax) -> list[tuple]:
    """饼图：matplotlib 的 Wedge 只有角度，百分比要自己算；标签在 ax.texts 里。"""
    out = []
    try:
        wedges = [p for p in ax.patches if hasattr(p, "theta1")]
        if not wedges:
            return out
        total = sum(max(w.theta2 - w.theta1, 0.0) for w in wedges) or 1.0
        labels = [t.get_text() for t in ax.texts]
        for i, w in enumerate(wedges):
            frac = max(w.theta2 - w.theta1, 0.0) / total
            name = labels[i] if i < len(labels) else f"slice_{i+1}"
            out.append((name, round(frac * 100, 1)))
    except Exception:
        pass
    return out


def figure_rows(fig, max_rows: int = 30) -> tuple[list[str], list[list]]:
    """把 figure 反解成 (表头, 数据行)。反解不出来返回 ([], [])。"""
    try:
        axes = fig.get_axes()
    except Exception:
        return [], []
    if not axes:
        return [], []
    ax = axes[0]

    pie = _pie(ax)
    if pie:
        rows = [[str(n), _fmt(v)] for n, v in pie]
        return ["category", "percent"], rows[:max_rows]

    bars = _bars(ax)
    series = _lines(ax) + _scatter(ax)
    if bars and not series:
        rows = [[str(n), _fmt(v)] for n, v in bars]
        return ["category", "value"], rows[:max_rows]
    if series:
        multi = len(series) > 1 or any(lbl for lbl, _ in series)
        if multi:
            rows = []
            for lbl, pts in series:
                for i, (x, y) in enumerate(pts):
                    rows.append([lbl or f"series_{len(rows)}", _fmt(x), _fmt(y)])
            return ["series", "x", "y"], rows[:max_rows]
        _, pts = series[0]
        rows = [[_fmt(x), _fmt(y)] for x, y in pts]
        return ["x", "y"], rows[:max_rows]
    if bars:
        rows = [[str(n), _fmt(v)] for n, v in bars]
        return ["category", "value"], rows[:max_rows]
    return [], []


def figure_chart_type(fig) -> str:
    """从 figure 推断图型：bar/line/pie/scatter/histogram，推断不出返回空串。"""
    try:
        axes = fig.get_axes()
    except Exception:
        return ""
    if not axes:
        return ""
    ax = axes[0]
    try:
        if _pie(ax):
            return "pie"
        bars = _bars(ax)
        series = _lines(ax) + _scatter(ax)
        if bars and not series:
            return "bar"
        if series:
            return "line" if ax.lines else "scatter"
        if bars:
            return "bar"
    except Exception:
        return ""
    return ""


def figure_to_csv(fig, max_rows: int = 30) -> str:
    """figure → CSV 文本。反解失败返回空字符串。"""
    header, rows = figure_rows(fig, max_rows=max_rows)
    if not header or not rows:
        return ""
    buf = io.StringIO()
    buf.write(",".join(header) + "\n")
    for r in rows:
        buf.write(",".join(str(c) for c in r) + "\n")
    return buf.getvalue().strip()


# ------------------------------------------------------------ 幻觉检测

def _numbers(text: str) -> list[float]:
    import re
    out = []
    for m in re.finditer(r"-?\d+(?:\.\d+)?", text or ""):
        try:
            out.append(float(m.group()))
        except ValueError:
            pass
    return out


def table_match_rate(csv_text: str, fig) -> float | None:
    """模型给的 CSV 里，有多少数字能在图里找到（容差 1% 或 0.5 绝对值）。

    只做**宽松**比对：目的是抓"整张表都是编的"这种大错，不是抓四舍五入。
    返回 None 表示没法比，两种情况：
    - 图反解不出数据 / CSV 里没有数字；
    - **饼图**：图里只有百分比，模型给的是原始计数，两者口径不同但都对，
      强行比对会把合理的表判成幻觉（pilot 实测 match=0.0 全是这个原因）。
    """
    if not (csv_text or "").strip():
        return None
    header, rows = figure_rows(fig, max_rows=200)
    if not rows:
        return None
    if "percent" in header:
        return None
    real = []
    for r in rows:
        for c in r:
            real.extend(_numbers(str(c)))
    if not real:
        return None
    claimed = _numbers(csv_text)
    # 表头/序号类数字（1,2,3...）会拉低命中率，但宽松阈值下影响有限
    if not claimed:
        return None

    hit = 0
    for v in claimed:
        for r in real:
            if abs(v - r) <= max(abs(r) * 0.01, 0.5):
                hit += 1
                break
    return round(hit / len(claimed), 3)
