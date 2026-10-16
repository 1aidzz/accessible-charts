"""目标二·原始可视化图表输入：读一张已有图表图片，提取数据后
重新生成符合无障碍标准的可视化，并产出四层 alt-text。

流程：VLM 看图 → 提取 {图型, 标题, 数据表, 读图描述} → 喂给代码生成模型
（带无障碍代码规则 + ===A11Y=== 契约，与 VisEval 管线同一套规范）→
matplotlib 渲染 → checker 反馈修复一轮 → 输出。

说明：这不是"美化原图"，而是**以原图为数据源重新作图**——原图无法"补"
无障碍属性，重新生成才能带上 Okabe-Ito 色板/大字号/edgecolor/分层描述。
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import traceback
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

from a11y.contract import (A11Y_CODE_RULES, A11Y_OUTPUT_SPEC,
                            A11Y_REPAIR_PROMPT, format_failures,
                            inject_svg_metadata, parse_a11y_fields)
from a11y.checker import run_chartability
from a11y.vlm_layer import _VL_MODEL, _client, _call

A11Y_ADDENDUM = A11Y_CODE_RULES + "\n" + A11Y_OUTPUT_SPEC

VLM_PROMPT = """Read this chart image carefully and extract its data.
Return strict JSON (no markdown):
{"chart_type":"bar|line|pie|scatter|histogram|stacked|other",
 "title":"the chart's title, or '' if none",
 "data_table_csv":"the exact data plotted, as CSV: one header row, one row per \
data point, max 30 rows. Use the real numbers you can read off the chart.",
 "description":"one plain sentence describing what the chart shows"}
If a value is unreadable, give your best estimate from the geometry."""


def extract_chart_from_image(png_path: str | Path) -> dict:
    """VLM 读原图 → 结构化数据。"""
    raw_bytes = Path(png_path).read_bytes()
    b64 = base64.b64encode(raw_bytes).decode()
    raw = _call(_client(), _VL_MODEL, [
        {"role": "user", "content": [
            {"type": "text", "text": VLM_PROMPT},
            {"type": "image_url",
             "image_url": {"url": f"data:image/png;base64,{b64}"}}]}],
        tokens=900,
        cache_key="i2c|" + _VL_MODEL + "|" + __import__("hashlib").sha1(raw_bytes).hexdigest())
    m = re.search(r"\{[\s\S]*\}", raw)
    try:
        d = json.loads(m.group(0)) if m else {}
    except json.JSONDecodeError:
        d = {}
    return {"chart_type": d.get("chart_type", ""),
            "title": d.get("title", ""),
            "data_table_csv": d.get("data_table_csv", ""),
            "description": d.get("description", "")}


GEN_SYSTEM = """You are an expert Python data-visualization engineer.
A dataframe called df is already loaded from the given CSV. Write ONE fenced \
python code block that draws the requested chart with matplotlib using ONLY df.
Do NOT call plt.show()."""

REPAIR = """Your code failed: {error}
Rewrite the FULL code, reply again with ONE fenced python code block."""


def _globals_for(df: pd.DataFrame) -> dict:
    import numpy as np
    return {"pd": pd, "np": np, "plt": plt, "df": df.copy()}


def _render(code: str, df: pd.DataFrame):
    """exec 生成代码并导出 SVG。返回 (ok, svg, err, figure_csv, figure_type)。

    figure_csv 是从 figure 反解的**图上真实数据**（a11y.extract），与 svg 同源、
    当场提取（后续轮次的 plt.close('all') 会销毁 figure）。alt-text 必须基于它
    而非输入原始表——否则 query 里带聚合（"按地点平均"）时，描述会说原始明细
    极值（如 65.7667），图上画的却是聚合后的均值（48.1），盲用户听到的与图不符。
    figure_type 是从 figure 推断的图型（bar/line/pie...）。
    """
    try:
        plt.close("all")
        exec(code, _globals_for(df))  # noqa: S102
        fig = plt.gcf()
        if not fig.get_axes():
            return False, "", "No axes were drawn.", "", ""
        fig_csv, fig_type = "", ""
        try:
            from a11y.extract import figure_to_csv, figure_chart_type
            # max_rows 要足够大：多系列图 N 系列×M 点是长表，默认 30 会把
            # 6 系列×7 点=42 行截成 30 行，丢掉最后一条系列 → 极值/系列数全错
            fig_csv = figure_to_csv(fig, max_rows=2000)
            fig_type = figure_chart_type(fig)
        except Exception:
            pass
        buf = io.StringIO()
        fig.savefig(buf, format="svg", bbox_inches="tight")
        return True, buf.getvalue(), None, fig_csv, fig_type
    except Exception:
        return False, "", traceback.format_exc(), "", ""


def _extract_code(raw: str) -> str | None:
    m = re.search(r"```(?:python)?\s*\n(.*?)```", raw, re.DOTALL)
    return m.group(1).strip() if m else None


def _check_on_final_svg(svg: str, fields: dict):
    """在"最终形态"的 SVG 上跑 checker：先把 title/desc 注入（与
    run_goal2._finish 的注入同构），再判 P2。

    若直接在模型渲染的原始 SVG 上跑，matplotlib 永远不产生 <title>/<desc>，
    P2 必挂——而 P2 是由后处理注入确定性保证的，模型再修代码也修不好，
    只会白白触发修复轮（2026-10-15 实测：两个 demo 都被 P2 强制拖进 repair，
    截断的 repair 响应反而把首轮完整的四件套覆盖成空）。
    返回 (checks, svg_with_metadata)。
    """
    desc = " ".join(t for t in (fields.get("readout_note", ""),
                                fields.get("takeaway", "")) if t.strip())
    svg_a = inject_svg_metadata(svg, fields.get("title", ""), desc)
    return run_chartability(svg_a, fields), svg_a


def generate_accessible_chart(table_csv: str, question: str,
                              max_repairs: int = 2) -> dict:
    """从数据表生成无障碍可视化（含 checker 反馈自修复）。

    返回 {ok, svg, fields, checks, code, err}。
    """
    # 默认 GLM-5.3-Flash：同任务 Qwen3.8-Flash 推理 11+ 分钟且常被服务端卡死，
    # GLM 稳定 4 秒级（2026-10-15 实测）。可用 A11Y_GEN_MODEL 环境变量覆盖。
    model = os.environ.get("A11Y_GEN_MODEL", "Ali-dashscope/GLM-5.3-Flash")
    df = pd.read_csv(io.StringIO(table_csv))
    cols = ", ".join(df.columns)
    base = (f"{GEN_SYSTEM}\n\nThe dataframe df has columns: {cols}. "
            f"Sample rows:\n{table_csv[:600]}\n\n"
            f"Request: {question}\n\n"
            + A11Y_ADDENDUM)
    client = _client(timeout=600)  # 代码生成：推理模型长思考，120s 必超时
    prompt, code, fields = base, None, {}
    best_svg, best_fields, best_fig_csv, best_fig_type, last_err = "", {}, "", "", None
    for attempt in range(max_repairs + 1):
        # 思考挡位：GLM-5.3-Flash 服务端明确"始终思考，不支持关闭"（传
        # disabled 直接 400 报错）。reasoning 会吃掉 max_tokens——4000 时实测
        # 总消耗 3954/4000，把 ===A11Y=== 块截断在 READOUT 中间（假低 CPR
        # 54.5%，且截断响应一旦写入缓存会被永久命中）。方案：thinking=low
        # 压低推理消耗 + max_tokens 提到 16000，实测 ===END=== 完整返回。
        raw = _call(client, model, [{"role": "user", "content": prompt}],
                    tokens=16000,
                    extra_body={"thinking": {"type": "low"}},
                    cache_key=f"gen|{model}|{attempt}|{prompt}",
                    complete_flag=lambda r: "===END===" in r)
        candidate = _extract_code(raw)
        if candidate is None:
            prompt += "\n\nReply again with ONE fenced python code block."
            continue
        # 复用主管线的容错解析器：旧版自写的 parse_a11y_block 要求必须有
        # ===END===，响应一旦在契约块处被截断就整块丢弃、四件套全空
        # （2026-10-15 实测 chartqa repair 轮：TITLE/READOUT/TAKEAWAY 完整
        # 但 TABLE 截断、缺 END，旧解析器直接返回 {}）。
        fields = parse_a11y_fields(raw)
        ok, svg, err, fig_csv, fig_type = _render(candidate, df)
        if not ok:
            code, last_err = candidate, err
            prompt += "\n\n" + REPAIR.format(error=(err or "")[:1200])
            continue
        best_svg, best_fields, best_fig_csv, best_fig_type, code = \
            svg, fields, fig_csv, fig_type, candidate
        # checker 反馈自修复：在注入元数据后的最终形态上判，避免 P2 假失败。
        # 只对 critical 失败触发修复轮——本项目消融实证（PROJECT_HANDOVER §4）：
        # 无障碍维度上反馈自修复 ≈ 0 增益，且模型重写时"修一处开两个新口子"，
        # 为 P6 这类非关键的"可能重叠"提示重跑，常见结局是把首轮完整的四件套
        # 换成更差的响应。非关键失败仅记录，不 repair。
        check_results, _ = _check_on_final_svg(svg, fields)
        fails = [r for r in check_results
                 if r.status == "fail" and r.critical]
        if fails and attempt < max_repairs:
            prompt = base + "\n\n" + A11Y_REPAIR_PROMPT.format(
                n=len(fails), fails=format_failures(fails))
            continue
        break
    if not best_svg:
        return {"ok": False, "err": last_err or "no runnable code", "code": code}
    final_checks, _ = _check_on_final_svg(best_svg, best_fields)
    checks = [{"id": r.id, "status": r.status, "critical": r.critical,
               "reason": r.reason[:120]}
              for r in final_checks]
    return {"ok": True, "svg": best_svg, "fields": best_fields,
            "figure_csv": best_fig_csv, "figure_type": best_fig_type,
            "checks": checks, "code": code}
