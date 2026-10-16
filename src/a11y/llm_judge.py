"""LLM-as-judge：给 ② 类"解释质量"检查补语义裁判。

背景
----
checker.py 里 U1（读图说明）/U2（标题）/A3（趋势描述）是"形式检查"：
- U1 只看 readout_note 够不够 3 个词；
- U2 只看 title 非空、不在弱词黑名单；
- A3 只看有没有提到真实类别 / 出现趋势词。

结果是废话也能满分（实测 "Read the chart." / "Chart 1." /
"The chart mentions East." 全 pass）。规则判断不了"这段说明到底有没有帮到
一个读屏用户、有没有和图对得上"——这是语言理解，必须交给 LLM 裁判。

本模块做最小原型：一次 LLM 调用同时评 title / purpose / takeaway 三项，
输出 0/1/2 分 + 一句理由。判分口径（rubric）：

U2 标题 title_quality
    0 = 占位/空话（"Chart 1." "Data." "Untitled"），或与图表内容无关
    1 = 点出了主题，但笼统（"Sales chart"）
    2 = 准确说出图表对象 + 关键维度/单位（"Sales (USD) by region, 2024"）

U1 读图说明 purpose_quality
    0 = 废话 / 命令句没信息量（"Read the chart." "Here is a chart."）
    1 = 大概说了这图是什么，但没说怎么读
    2 = 明确告诉读屏用户"这图在比较什么、怎么读"（比较方向 / 坐标含义 /
        该看哪个极端）

A3 趋势描述 takeaway_accuracy
    0 = 与图不符 / 编造类别或数值 / 没说任何特征（"The chart mentions East."）
    1 = 提到图上真实内容，但浅、没点出趋势或极值
    2 = 准确点出图上真实的趋势/极值/对比，且不编造（会拿 figure_csv 核对）

pass 阈值：>=1（即"不是废话/不是编造"就过，0 才 fail）。阈值后续用 κ 标定。

用法（最小原型）：
    cd src && python -m a11y.llm_judge            # 跑内置 demo
    或 import judge_texts(...) 接进 pipeline。
"""

from __future__ import annotations

import json
import re

RUBRIC = """You are a strict accessibility reviewer for screen-reader users.
You are given a chart's underlying data (CSV) and three short texts that were
auto-generated to describe the chart. Score each text 0/1/2.

Scoring:
TITLE (title_quality):
  0 = placeholder/empty ("Chart 1.", "Data.", "Untitled") or off-topic
  1 = names the subject but vague ("Sales chart")
  2 = names the object AND key dimension/unit ("Sales (USD) by region, 2024")

PURPOSE NOTE (purpose_quality):
  0 = filler / a command with no information ("Read the chart.", "Here is a chart.")
  1 = roughly says what the chart is, but not how to read it
  2 = tells a blind reader WHAT is compared and HOW to read it (which axis means
      what, which extreme to look at, comparison direction)

TAKEAWAY (takeaway_accuracy):
  0 = contradicts the data / invents categories or numbers / describes no feature
  1 mentions something real but stays shallow (no trend/extreme/comparison)
  2 = correctly states a real trend / maximum / minimum / comparison that matches
      the CSV numbers; do NOT give credit to invented categories or values

Be strict: a fluent-sounding sentence that carries no information scores 0.
Verify every number/category in the takeaway against the CSV; if it does not
match, score 0.

Return ONLY a compact JSON object, no prose, no markdown fence:
{"title_quality": int, "title_reason": "...",
 "purpose_quality": int, "purpose_reason": "...",
 "takeaway_accuracy": int, "takeaway_reason": "..."}
"""


def _build_llm(model: str, base_url: str = "http://localhost:11434"):
    from langchain_ollama import ChatOllama
    return ChatOllama(model=model, base_url=base_url,
                      temperature=0.0, reasoning=False, num_ctx=8192,
                      num_predict=300)


def judge_texts(llm, figure_csv: str, title: str,
                readout_note: str, takeaway: str) -> dict:
    """一次调用评三项。返回 {check_id: {score, pass, reason}}。

    pass = score >= 1（0 才 fail）。解析失败一律按"存疑=不判"返回 na，
    绝不因为 JSON 崩了就误杀。
    """
    user = (
        f"CHART DATA (CSV):\n{figure_csv or '(none)'}\n\n"
        f"TITLE: {title or '(empty)'}\n"
        f"PURPOSE NOTE: {readout_note or '(empty)'}\n"
        f"TAKEAWAY: {takeaway or '(empty)'}\n"
        f"\nScore the three texts now."
    )
    resp = llm.invoke([
        {"role": "system", "content": RUBRIC},
        {"role": "user", "content": user},
    ])
    raw = getattr(resp, "content", str(resp))
    m = re.search(r"\{[\s\S]*\}", raw)
    if not m:
        return {k: {"score": None, "pass": None, "reason": "unparseable",
                    "raw": raw[:200]}
                for k in ("U2", "U1", "A3")}
    try:
        d = json.loads(m.group(0))
    except json.JSONDecodeError:
        return {k: {"score": None, "pass": None, "reason": "json error",
                    "raw": m.group(0)[:200]}
                for k in ("U2", "U1", "A3")}

    def pack(q, rkey):
        s = d.get(q)
        if not isinstance(s, int):
            return {"score": None, "pass": None, "reason": d.get(rkey, "")[:80]}
        return {"score": s, "pass": s >= 1, "reason": str(d.get(rkey, ""))[:120]}

    return {
        "U2": pack("title_quality", "title_reason"),
        "U1": pack("purpose_quality", "purpose_reason"),
        "A3": pack("takeaway_accuracy", "takeaway_reason"),
    }


# ---------------- 最小 demo：废话 vs 诚实 vs 编造 --------------------
def _demo():
    llm = _build_llm("qwen2.5-coder:7b-instruct")
    fig = "category,value\nEast,200\nWest,50\nNorth,120"
    cases = [
        ("废话三件套",
         "Chart 1.", "Read the chart.", "The chart mentions East."),
        ("诚实三件套",
         "Sales (USD) by region, 2024",
         "Bar chart: compare bar heights across four regions; taller bar = higher sales.",
         "East has the highest sales at 200, West the lowest at 50."),
        ("编造极值",
         "Sales by region.",
         "Bar chart comparing regions.",
         "The highest is blue whale at 999."),
    ]
    for name, title, note, take in cases:
        print(f"\n===== {name} =====")
        out = judge_texts(llm, fig, title, note, take)
        for cid, r in out.items():
            print(f"  {cid}: score={r['score']} pass={r['pass']} | {r['reason']}")


if __name__ == "__main__":
    _demo()
