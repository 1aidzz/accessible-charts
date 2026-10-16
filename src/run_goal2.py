"""目标二端到端 runner：数据（或原始图表图片）→ 无障碍可视化 + 四层 alt-text。

两种输入模式：
  --csv data/vega/barley.json --query "平均产量按地点画柱状图"   # 给定数据
  --image data/chartqa/xxx.png                                  # 原始图表图片
  --demo                                                        # 内置两个演示

输出：logs/goal2/<slug>/ 下
  chart.svg      注入无障碍元数据 + 分层描述的最终图
  alt_text.json  四层描述（含 L1/L2 确定性核验结果）
  report.json    checker 全量检查 + 两阶段摘要

用法：cd src && python run_goal2.py --demo
需要环境变量 OPENAI_API_KEY；VLM/代码生成走 a11y.vlm_layer 的磁盘缓存。
"""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "agents"))

from a11y.alttext import combine, generate_alt_text, inject_alt, verify_layers  # noqa: E402
from a11y.checker import (  # noqa: E402
    compute_cpr, compute_llm_cpr, run_chartability)
from agents.image_to_chart import (  # noqa: E402
    extract_chart_from_image, generate_accessible_chart)

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "logs/goal2"


def _slug(s: str) -> str:
    import re
    return re.sub(r"[^\w\-]+", "_", s)[:40] or "sample"


def run_data_mode(csv_path: Path, query: str, out_dir: Path) -> dict:
    """给定数据 → 生成无障碍图 + 分层描述。"""
    df = pd.read_csv(csv_path) if csv_path.suffix == ".csv" \
        else pd.DataFrame(json.loads(csv_path.read_text(encoding="utf-8")))
    table_csv = df.to_csv(index=False)
    res = generate_accessible_chart(table_csv, query)
    if not res["ok"]:
        return _dump_fail({"ok": False, "stage": "generate",
                           "err": res["err"]}, out_dir)
    return _finish(res["svg"], res["fields"], query,
                   chart_type=res.get("figure_type", ""),
                   table_csv=table_csv, out_dir=out_dir,
                   figure_csv=res.get("figure_csv", ""))


def run_image_mode(png_path: Path, query: str, out_dir: Path) -> dict:
    """原始图表图片 → 提取数据 → 重新生成无障碍图 + 分层描述。"""
    ext = extract_chart_from_image(png_path)
    table = ext.get("data_table_csv") or ""
    if not table.strip():
        return _dump_fail({"ok": False, "stage": "extract",
                           "err": "VLM 未提取到数据表"}, out_dir)
    q = query or f"Recreate this {ext.get('chart_type') or ''} chart accessibly: " \
                 f"{ext.get('description', '')}"
    res = generate_accessible_chart(table, q)
    if not res["ok"]:
        return _dump_fail({"ok": False, "stage": "generate",
                           "err": res["err"]}, out_dir)
    chart_type = ext.get("chart_type", "") or res.get("figure_type", "")
    return _finish(res["svg"], res["fields"], q, chart_type=chart_type,
                   table_csv=table, out_dir=out_dir, source=str(png_path),
                   extraction=ext, figure_csv=res.get("figure_csv", ""))


def _dump_fail(res: dict, out_dir: Path) -> dict:
    """失败也落盘，否则现场丢失只能靠 stdout 截断的 print 诊断。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")
    return res

def _finish(svg: str, fields: dict, question: str, chart_type: str,
            table_csv: str, out_dir: Path, figure_csv: str = "", **extra) -> dict:
    """公共收尾：四层描述 → 注入 SVG → checker 终检 → 落盘。

    alt 的数据源用 figure_csv（从 figure 反解的**图上真实数据**），拿不到才
    退回输入表 table_csv。这样描述的极值/趋势与图严格一致（聚合 query 场景下
    输入表是明细、图上是聚合值，用输入表会让描述与图对不上）。

    审计口径（2026-10-16 修正"审 A 交付 B"错位）：
    SVG <desc> 里装的是 alt 四层文本（生成线 B），此前 checker 终检读的却是
    fields 里代码生成模型顺手写的三件套（生成线 A）——两套文字来自两次独立
    LLM 调用，可能互相矛盾，而读屏用户听到的是 B、被审的却是 A。
    现在用 alt 的产物覆盖语义字段后再终检，保证 checker 审的就是
    读屏用户实际听到的内容：
      - readout_note ← L1（U1 审读图说明 = desc 里的构造层）
      - takeaway     ← L2（A3 审极值/趋势 = desc 里的统计层）
      - data_table_csv ← alt_data（C1 对账源与 L2 同源）
      - figure_csv   ← 图上反解数据（激活 A3/C1 已有的内容级对账——此前
        figure_csv 从未进过 ctx，对账拿 A 自报表对 A 自报表，形同虚设）
    title 不覆盖：<title> 节点本来就是 A 的标题，U2 审的与用户听到的一致；
    "标题断言 vs 数据"对账（如 barley "Duluth the Lowest" 硬伤）是后续可补项。
    L3/L4 是解读层：规则层无对应条目，LLM 质量层（A11Y_LLM_JUDGE=0 可关）
    对其打 0/1/2 分；L3/L4 为空（确定性兜底）时标 na 不调 LLM。
    """
    alt_data = (figure_csv or table_csv or "")
    alt = generate_alt_text(fields.get("title", ""), alt_data, question, chart_type)
    alt["verify"] = verify_layers(alt, alt_data)
    svg_final = inject_alt(svg, alt, alt_data)
    check_ctx = dict(fields)
    check_ctx["readout_note"] = (alt.get("l1_construction") or "").strip()
    check_ctx["takeaway"] = (alt.get("l2_statistics") or "").strip()
    check_ctx["data_table_csv"] = alt_data
    if figure_csv:
        check_ctx["figure_csv"] = figure_csv
    # LLM 质量层接线（需求 2026-10-16）：先落盘 chart.svg（G0/U1 截图依赖
    # _svg_path 存在），再把四层文本与用户原始问题递给 checker——
    # G0 可读门 + U1/U2/A3/L3/L4 五项 0/1/2 打分，失败自动 skip 不炸。
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "chart.svg").write_text(svg_final, encoding="utf-8")
    check_ctx["_vlm"] = True
    check_ctx["_svg_path"] = str(out_dir / "chart.svg")
    check_ctx["l3_patterns"] = (alt.get("l3_patterns") or "").strip()
    check_ctx["l4_context"] = (alt.get("l4_context") or "").strip()
    check_ctx["question"] = question
    checks = run_chartability(svg_final, check_ctx)
    cpr = compute_cpr(checks)
    cpr_llm = compute_llm_cpr(checks)
    # LLM 分数回写进 alt_text.json 的 verify 块（向后兼容：老字段原样保留）
    alt["verify"]["llm_scores"] = {k: cpr_llm[k]
                                   for k in ("u1", "u2", "a3", "l3", "l4")
                                   if k in cpr_llm}
    # 把 l3_l4 从 "pending_llm_judge" 更新成真实结果：
    # 两项都有分数 → llm_scored(l3=..,l4=..)；
    # 文本非空但 LLM 没打成（无 key/超时/JSON 挂）→ llm_skipped；
    # 文本本身为空（deterministic 兜底）→ empty_not_judged。
    _l3, _l4 = cpr_llm.get("l3"), cpr_llm.get("l4")
    if _l3 is not None and _l4 is not None:
        alt["verify"]["l3_l4"] = f"llm_scored(l3={_l3},l4={_l4})"
    elif alt.get("l3_patterns") or alt.get("l4_context"):
        alt["verify"]["l3_l4"] = "llm_skipped"
    else:
        alt["verify"]["l3_l4"] = "empty_not_judged"
    (out_dir / "alt_text.json").write_text(
        json.dumps(alt, ensure_ascii=False, indent=2), encoding="utf-8")
    report = {
        "cpr": {"cpr_all": cpr.cpr_all, "cpr_critical": cpr.cpr_critical,
                "applicable": cpr.applicable, "passed": cpr.passed,
                "failed": cpr.failed, "na": cpr.na},
        "cpr_llm": cpr_llm,
        "failures": [{"id": r.id, "reason": r.reason[:120]}
                     for r in checks
                     if r.status == "fail" and r.layer != "VLM"],
        "alt_text_combined": combine(alt),
        "alt_data_source": "figure" if figure_csv else "input_table",
        "checker_text_source": "alt_layers",
        "a_fields_discarded": {k: fields.get(k, "")
                               for k in ("readout_note", "takeaway")},
        **extra,
    }
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    report["ok"] = True
    report["out_dir"] = str(out_dir)
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv")
    ap.add_argument("--image")
    ap.add_argument("--query", default="")
    ap.add_argument("--demo", action="store_true")
    args = ap.parse_args()

    if args.demo:
        barley = ROOT / "data/vega/barley.json"
        pngs = sorted((ROOT / "data/chartqa").glob("*.png"))
        jobs = [("demo_barley_data", barley, "Average barley yield by site as a bar chart")]
        if pngs:
            jobs.append(("demo_chartqa_image", pngs[0], ""))
        for slug, src, q in jobs:
            print(f"=== {slug} ({'image' if src.suffix=='.png' else 'data'}) ===", flush=True)
            r = run_image_mode(src, q, OUT / slug) if src.suffix == ".png" \
                else run_data_mode(src, q, OUT / slug)
            print(json.dumps({k: r.get(k) for k in ("ok", "stage", "err", "cpr",
                                                    "out_dir")},
                             ensure_ascii=False, default=str)[:400], flush=True)
        return

    if args.image:
        r = run_image_mode(Path(args.image), args.query, OUT / _slug(Path(args.image).stem))
    elif args.csv:
        r = run_data_mode(Path(args.csv), args.query, OUT / _slug(Path(args.csv).stem))
    else:
        ap.error("需要 --csv / --image / --demo 之一")
    print(json.dumps(r, ensure_ascii=False, default=str)[:500])


if __name__ == "__main__":
    main()
