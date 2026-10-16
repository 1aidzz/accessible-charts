"""Phase 5：把「无障碍」从提示词里搬出来，交给确定性后处理。

动机（2026-10-03，来自 Phase 4 trade-off 的结论）
------------------------------------------------
给模型加无障碍提示词，CPR 从 0.591 涨到 0.894，但 VisEval 正确性从 67.63%
掉到 58.47%，掉分几乎全在 data check（25%→37%）——**不是画不出来，是画错了**。
归因是一次性往提示词里塞太多要求（色板、字号、描边、三件套、数据表），
模型的注意力被格式占满，顾不上看数据。

但这里面有一件事**根本不需要模型做**：颜色、字号、描边、ARIA 元数据、
数据表、以及"图上谁是最大"这种结论，全都能在**渲染时**由程序确定性地保证。

关键实现方式：**这段注入代码在模型生成之后才拼上去，模型从头到尾看不见它。**
所以正确性不受影响不是实验测量出来的结论，而是结构性保证——模型收到的
prompt 与基线逐字相同，产出的代码也就逐字相同。

三件法宝
--------
1. ``inject_rc_params`` —— matplotlib rcParams：色盲安全色板 + 字号 + 描边。
2. ``build_postprocessed_fields`` —— 缺失的三件套用规则兜底（含从 figure
   反解出来的数据表）。
3. ``template_takeaway`` —— 从图里真实数据生成的结论句。它让 A3 的内容级
   检查必然通过（说的就是图上的真值），顺便把 Phase 4 里那条"说了立刻xxx，
   图上其实是 xxx"的幻觉问题从根上绕开。

用法
----
    from postprocess import inject_rc_params, build_postprocessed_fields

    code = inject_rc_params(model_code)      # 渲染前拼上去，模型不知道
    fields = build_postprocessed_fields({}, svg=svg, nl_query=q, chart=c,
                                        figure_rows=(header, rows))
"""

from __future__ import annotations

# ============================================================ 配色

# 候选色相：先把「不看文档也能一眼认出是不同颜色」的基色列出来，
# 具体深浅交给 select_palette 通过对比度门槛去定。
_PALETTE_CANDIDATES = (
    "#0072B2",  # 蓝
    "#D55E00",  # 朱红
    "#009E73",  # 绿
    "#785EF0",  # 紫（IBM 无障碍色板）
    "#CC79A7",  # 洋红
    "#F0E442",  # 黄（往往会被筛掉：压暗后跟橙分不开）
    "#56B4E9",  # 天蓝
    "#8C564B",  # 棕
    "#117733",  # 深绿
    "#882255",  # 酒红
    "#332288",  # 靛蓝
    "#DDCC77",  # 砂黄
)

# Okabe-Ito：色觉障碍（红绿色盲）下依然两两可分的经典八色，色相设计过。
# 问题是其中三色对白底对比度不达标（这是 Chartability P1 与 P7 的真实张力）：
#   #E69F00 橙 2.25:1、#56B4E9 天蓝 2.31:1、#F0E442 黄 1.07:1 —— 都低于 3:1。
# 所以下面按「保留色相、压暗亮度」的思路处理。
OKABE_ITO = (
    "#0072B2", "#E69F00", "#009E73", "#56B4E9",
    "#F0E442", "#D55E00", "#CC79A7", "#999999",
)

_BG = (255, 255, 255)
_TARGET_CONTRAST = 3.2   # 留一点余量，别贴着 3.0 边走


def _hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _rgb(h: str) -> tuple[int, int, int]:
    h = (h or "").strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    def ch(c: float) -> float:
        c /= 255.0
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    return 0.2126 * ch(rgb[0]) + 0.7152 * ch(rgb[1]) + 0.0722 * ch(rgb[2])


def _contrast(rgb: tuple[int, int, int], bg: tuple[int, int, int] = _BG) -> float:
    def lum(c):
        return _relative_luminance(c)
    a, b = lum(rgb), lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def darken_to_contrast(rgb: tuple[int, int, int],
                       target: float = _TARGET_CONTRAST) -> tuple[int, int, int]:
    """按比例压暗到与白底对比度 ≥ target。按比例缩放能大致保住色相。"""
    if _contrast(rgb) >= target:
        return rgb
    lo, hi = 0.0, 1.0
    for _ in range(24):  # 二分，24 次足够精确到 1/255
        mid = (lo + hi) / 2
        cand = tuple(max(0, int(round(v * mid))) for v in rgb)
        if _contrast(cand) >= target:
            lo = mid
        else:
            hi = mid
    out = tuple(max(0, int(round(v * lo))) for v in rgb)
    # 极端情况下（纯黑附近）缩放会退化成全黑，兜一下：至少留一点点色相差别
    return out if any(out) else (0, 0, 0)


def build_palette(base: tuple[str, ...] = OKABE_ITO) -> list[str]:
    """压暗到全部满足对比度门槛，并去掉压暗后重复的颜色。"""
    out: list[str] = []
    seen: set[str] = set()
    for h in base:
        c = _hex(darken_to_contrast(_rgb(h)))
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def select_palette(base: tuple[str, ...] = _PALETTE_CANDIDATES,
                   min_contrast: float = _TARGET_CONTRAST,
                   min_delta_e: float = 12.0) -> list[str]:
    """贪心选一组**同时**满足 P1（对比度）和 P7（色觉可分辨）的颜色。

    为什么不能直接用压暗后的 Okabe-Ito：把 #F0E442 黄压到能过 3:1 之后，
    它和压暗橙 #C08400 在红色盲仿真下只剩 ΔE≈8.8，**P1 是过了，P7 又塌了**。
    这就是 Chartability 里那条真实张力，量化之后才看得出代价有多大。

    所以这里按「加进去会不会跟已选的颜色在三种色觉下撞车」来贪心筛选，
    撞车就丢掉这个候选。丢掉的通常都是某个中间色相，代价是调色板颜色变少，
    但这比"贴图去年看不出区别"要好。
    """
    accepted: list[str] = []
    try:
        from checker import _simulate_cvd, _delta_e_lab
    except ImportError:  # 包外直接用时兜底
        from .checker import _simulate_cvd, _delta_e_lab

    for h in base:
        rgb = darken_to_contrast(_rgb(h))
        if _contrast(rgb) < min_contrast:
            continue
        ok = True
        for other in accepted:
            OR, NR = _rgb(other), rgb
            for cvd in ("protanopia", "deuteranopia", "tritanopia"):
                if _delta_e_lab(_simulate_cvd(OR, cvd), _simulate_cvd(NR, cvd)) < min_delta_e:
                    ok = False
                    break
            if not ok:
                break
        if ok:
            accepted.append(_hex(rgb))
    return accepted


PALETTE = select_palette()


# ============================================================ rcParams 注入

_RC_BLOCK = '''
# ---- 无障碍渲染后处理（确定性保证，不依赖模型，模型也看不到这一段）----
try:
    import matplotlib as _a11y_mpl
    from cycler import cycler as _a11y_cycler
    _a11y_mpl.rcParams.update({
        "axes.prop_cycle": _a11y_cycler(color=__PALETTE__),
        "font.size": 13,
        "axes.titlesize": 15,
        "axes.labelsize": 13,
        "xtick.labelsize": 12,
        "ytick.labelsize": 12,
        "legend.fontsize": 12,
        "figure.titlesize": 15,
        "lines.linewidth": 2.4,
        "lines.markersize": 8,
        "patch.linewidth": 1.2,
        "patch.edgecolor": "#1A1A1A",
        "axes.linewidth": 1.0,
        "axes.edgecolor": "#333333",
        "figure.facecolor": "white",
        "savefig.facecolor": "white",
    })
except Exception:
    pass
# ---- 后处理结束 ----
'''


def rc_block(palette: list[str] | None = None) -> str:
    """生成 rcParams 注入代码。

    用占位符替换而不是 str.format —— 块里有字典字面量，花括号会跟
    format 的语法打架（实测直接 KeyError）。
    """
    return _RC_BLOCK.replace("__PALETTE__", repr(list(palette or PALETTE)))


def inject_rc_params(code: str) -> str:
    """把 rcParams 块拼到代码最前面。

    必须**在新建 figure 之前**执行，否则 prop_cycle 对已经画好的 axes 不生效。
    模型几乎一定会在后面自己调 plt.subplots()，所以放最前面就对了。
    """
    code = code or ""
    return rc_block() + "\n" + code


# ============================================================ 数据驱动的兜底结论

def _fmt(v) -> str:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    if f != f or f in (float("inf"), float("-inf")):
        return str(v)
    if abs(f - round(f)) < 1e-9:
        return str(int(round(f)))
    return f"{f:.3g}"


def extremes(header: list[str], rows: list[list]) -> tuple[tuple[str, float],
                                                           tuple[str, float]] | None:
    """从 figure 反解的行里找出最大/最小：返回 ((标签, 值), (标签, 值))。

    header 有四种（category,value / x,y / series,x,y / category,percent），
    数值列恒为最后一列，其余非数字单元格拼起来当标签。
    """
    if not rows:
        return None
    parsed: list[tuple[str, float]] = []
    for r in rows:
        nums, labels = [], []
        for c in r:
            try:
                nums.append(float(str(c).strip()))
            except (TypeError, ValueError):
                labels.append(str(c).strip())
        if not nums:
            continue
        parsed.append((" ".join(labels) or _fmt(nums[0]), nums[-1]))
    if len(parsed) < 2:
        return None
    top = max(parsed, key=lambda kv: kv[1])
    bottom = min(parsed, key=lambda kv: kv[1])
    return top, bottom


def template_takeaway(header: list[str], rows: list[list]) -> str:
    """从真实数据生成一句结论。

    这句话**必然**能被 A3 的内容级检查判通过 —— 因为它说的就是图上真实的最大
    最小值。这也正是它存在的意义：模型编结论时会把十二月说成最大（而图上只有
    二月和三月），程序照着数据写就不会。
    """
    ex = extremes(header, rows)
    if not ex:
        return ""
    (tname, tval), (bname, bval) = ex
    unit = "percent" if "percent" in (header or []) else ""
    suffix = f" {unit}" if unit else ""
    return (f"The highest value is {tname} at {_fmt(tval)}{suffix}, "
            f"and the lowest is {bname} at {_fmt(bval)}{suffix}.")


def build_postprocessed_fields(fields: dict, *, svg: str, nl_query: str,
                               chart: str, figure_csv: str = "",
                               header: list[str] | None = None,
                               rows: list[list] | None = None) -> tuple[dict, list[str]]:
    """补齐三件套。优先用模型写的，没有才由后处理生成。

    返回 (补齐后的字段, 本次补了哪些)。补了哪些要记账——报告里要能说清
    "这个分数里有多少是我们替模型做的"。
    """
    try:
        from .contract import build_injected
    except ImportError:  # 包外直接用时兜底
        from contract import build_injected

    out, filled = build_injected(fields, svg=svg, nl_query=nl_query,
                                 chart=chart, data_table_csv=figure_csv)
    # takeaway 允许升级：模型写的 takeaway 可能跟图对不上（这是 A3 内容级检查
    # 抓到的 30 处漏报），而模板句是照着真数据写的，一定能通过内容校验。
    # 只在模型压根没给、或者给的完全没有数据支撑时才替换。
    if not (fields.get("takeaway") or "").strip():
        generated = template_takeaway(header or [], rows or [])
        if generated:
            out["takeaway"] = generated
            if "takeaway" not in filled:
                filled.append("takeaway")
    return out, filled


# ============================================================ 自检

def palette_report() -> str:
    """把色板的关键指标打成文本，方便核对 P1（对比度）和 P7（色觉安全）。"""
    lines = [f"{'color':<10}{'对比度':>8}"]
    for c in PALETTE:
        lines.append(f"{c:<10}{_contrast(_rgb(c)):>8.2f}")
    return "\n".join(lines)


if __name__ == "__main__":
    print(palette_report())
