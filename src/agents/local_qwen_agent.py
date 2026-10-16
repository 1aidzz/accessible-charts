"""VisEval Agent，后端接本地 OpenAI 兼容服务（Ollama / vLLM / llama.cpp）。

基于 viseval/agent.py 的真实接口实现：
    generate(nl_query, tables, config) -> (code, context)
    execute(code, context, log_name)   -> ChartExecutionResult(status, svg_string, error_msg)

Evaluator 会自行把 svg_string 写回 context，并从 SVG 反解出 encoding / chart，
所以这里不需要、也不应该手动构造这些字段。

注意：表格是 CSV 文件路径列表（dataset/databases/<db>/*.csv）。
"""

from __future__ import annotations

import io
import re
import sys
import time
import traceback
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import _compat  # noqa: F401  必须在 import viseval 之前

import matplotlib

matplotlib.use("Agg")  # 必须在 import pyplot 之前
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

from a11y.checker import run_chartability  # noqa: E402
from a11y.contract import (  # noqa: E402
    A11Y_CODE_RULES,
    A11Y_OUTPUT_SPEC,
    A11Y_REPAIR_PROMPT,
    format_failures,
    has_any_field,
    inject_svg_metadata,
    parse_a11y_fields,
)
from viseval.agent import Agent, ChartExecutionResult  # noqa: E402

SYSTEM_PROMPT = """You are an expert Python data-visualization engineer.
Write Python code that draws exactly the chart requested, using matplotlib.

Hard rules:
- The data tables are ALREADY loaded as pandas DataFrames named: {names}
- Never read files. Never call plt.show(). Never call plt.savefig().
- Use `fig, ax = plt.subplots()` and draw on `ax`.
- Always set axis labels. Add a legend when there is more than one series.
- Follow sorting and aggregation instructions literally.
- Reply with ONE fenced python code block, and nothing else.
"""

REPAIR_PROMPT = """Your code raised this error when executed:

{error}

Rewrite the full code so it runs. Reply with ONE fenced python code block, and nothing else."""

NO_BLOCK_PROMPT = """You did not return a fenced python code block.
Reply again with ONE fenced python code block containing the complete code."""

# ---------------------------------------------------------------------------
# 无障碍附加段（Phase 3）：拼在 ours 提示词后面
# 由两部分组成：代码层面的硬要求 + 输出契约（三件套）
# ---------------------------------------------------------------------------
A11Y_ADDENDUM = A11Y_CODE_RULES + "\n" + A11Y_OUTPUT_SPEC

# 要求模型补齐缺失的元数据块（模型只给了代码、没给 ===A11Y=== 时用）
A11Y_MISSING_PROMPT = """You did not output the ===A11Y=== metadata block.
Keep the code as it is and reply again with ONE fenced python code block
followed by the ===A11Y=== block (TITLE / READOUT / TAKEAWAY / TABLE)."""

# ---------------------------------------------------------------------------
# 官方（Chat2VIS）风格：照抄 microsoft/VisEval 的 examples/agent/chat2vis.py
# 目的：让分数能和 VisEval 论文直接对比。
# 与「ours」的关键差异：会给 category / date 列列出最多 10 个真实取值。
# ---------------------------------------------------------------------------
MAXIMUM_SAMPLES = 10

# 逐字照抄官方（含 "df" 这个名字、data_file.csv 这个假文件名）
OFFICIAL_TEMPLATE = '''"""Use a dataframe called df from data_file.csv with columns {columns}.

{columns_description}

Label the x and y axes appropriately. Add a title. Set the fig suptitle as empty.

Using Python version 3.9.12 and library {library}, create a script using the dataframe df to graph the following: {nl_query}.
"""

{pre_code}
'''

OFFICIAL_PRE_CODE = """import pandas as pd
import matplotlib.pyplot as plt
{import_statements}
fig,ax = plt.subplots(1,1,figsize=(10,4))
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)

df=df_nvBenchEval.copy()
"""


def _column_description(series: pd.Series) -> str:
    """逐列生成描述，逻辑对齐 Chat2VIS.table_format()。"""
    name = series.name
    dtype = series.dtype
    kind = None

    if pd.api.types.is_bool_dtype(series):
        kind = "bool"
    elif pd.api.types.is_numeric_dtype(series):
        kind = "numeric"
    elif pd.api.types.is_datetime64_any_dtype(series):
        kind = "date"
    elif isinstance(dtype, pd.CategoricalDtype):
        kind = "category"
    elif pd.api.types.is_string_dtype(series) or dtype == object:
        # 先试着当日期解析
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                pd.to_datetime(series, errors="raise")
            kind = "date"
        except Exception:
            try:
                ratio = series.nunique() / max(len(series), 1)
            except Exception:
                ratio = 1.0
            kind = "category" if ratio < 0.5 else "string"
    else:
        kind = "other"

    if kind == "numeric":
        return f"The column '{name}' is type {dtype} and contains numeric values."
    if kind == "bool":
        return f"The column '{name}' is type {dtype} and contains boolean values."

    if kind in ("date", "category"):
        try:
            non_null = series[series.notnull()].unique()
            n = min(MAXIMUM_SAMPLES, len(non_null))
            samples = pd.Series(non_null).sample(n, random_state=42).tolist()
            values = "'" + "', '".join(str(s) for s in samples) + "'"
            tail = " etc." if n < len(non_null) else "."
            return f"The column '{name}' has {kind} values {values}{tail}"
        except Exception:
            return f"The column '{name}' has {kind} values."

    return f"The column '{name}' is {dtype} type."


def describe_table_official(csv_path, df_name="df") -> str:
    """生成官方风格的完整提示词片段（含列名列表 + 逐列描述）。"""
    df = pd.read_csv(csv_path)
    columns = "'" + "', '".join(str(c) for c in df.columns) + "'"
    description = " ".join(_column_description(df[c]) for c in df.columns)
    return columns, description


CODE_BLOCK = re.compile(r"```(?:python|py)?[ \t]*\r?\n(.*?)```", re.DOTALL | re.IGNORECASE)

# 模型经常把 ===A11Y=== 块写进 python 围栏**里面**（qwen2.5-coder:7b 实测必犯），
# 于是抠出来的"代码"末尾拖着一段非 Python 文本 → SyntaxError，
# 且自修复拿到这个 traceback 也修不好（它不知道自己格式写错了）。
# 这里一律在 A11Y 标记处截断：宁可丢掉元数据，也不能让代码跑不起来。
_A11Y_MARK = re.compile(r"^\s*=+\s*A11Y\s*=+", re.MULTILINE | re.IGNORECASE)


def _cut_at_a11y(code: str) -> str:
    m = _A11Y_MARK.search(code or "")
    return code[: m.start()].strip() if m else code


def extract_code(text: str):
    """从模型输出里抠出代码。抠不到返回 None。"""
    m = CODE_BLOCK.search(text or "")
    if m:
        return _cut_at_a11y(m.group(1).strip())
    # 兜底：模型没加围栏但内容看着就是代码
    if "plt." in (text or ""):
        return _cut_at_a11y(text.strip())
    return None


def describe_table(csv_path: str, nrows: int = 5) -> str:
    """只把 schema + 少量样例行喂给模型，不要塞整张表。"""
    df = pd.read_csv(csv_path, nrows=nrows)
    cols = ", ".join(f"{c} ({df[c].dtype})" for c in df.columns)
    sample = df.head(3).to_string(index=False)
    return f"Table `{Path(csv_path).stem}`\nColumns: {cols}\nSample rows:\n{sample}"


class LocalLLMAgent(Agent):
    def __init__(self, llm, config: dict = None):
        self.llm = llm
        self.config = config or {}
        self.max_repair = int(self.config.get("max_repair", 2))
        self.nrows = int(self.config.get("nrows", 5))
        # official = 对齐 Chat2VIS（可跟论文比）；ours = 表头+3 行样例
        self.prompt_style = str(self.config.get("prompt_style", "official"))

        # ---- Phase 3：无障碍三件套相关开关 ----
        # a11y_prompt：提示词里要不要加无障碍要求 + 输出契约
        self.a11y_prompt = bool(self.config.get("a11y_prompt", False))
        # a11y_repair：代码跑通之后，要不要把 Chartability 的失败项回传要求重写。
        # 这是 Phase 3 的核心杠杆。注意它和 max_repair 是**独立预算**：
        #   max_repair 修「代码报错」，a11y_repair 修「无障碍不达标」。
        # 消融里必须留一组「预算相同但只修代码」的对照（C0），
        # 否则无法区分提升来自"反馈内容"还是"多调了几次模型"。
        self.a11y_repair = int(self.config.get("a11y_repair", 0))
        # contract_mode: generated=三件套必须来自模型（主考察对象）
        #                injected=缺失时由规则兜底（能力上界，不是模型水平）
        self.contract_mode = str(self.config.get("contract_mode", "generated"))

    # ---------------- 执行环境 ----------------
    @staticmethod
    def _make_globals(tables):
        g = {"plt": plt, "pd": pd}
        for i, t in enumerate(tables, start=1):
            g[f"df{i}"] = pd.read_csv(t)
        if "df1" in g:
            g["df"] = g["df1"]  # 单表场景的常见别名
            # 官方 pre_code 里写的是 df=df_nvBenchEval.copy()，得提供这个名字
            g["df_nvBenchEval"] = g["df1"]
        return g

    def _try_run(self, code: str, tables) -> tuple[bool, str | None]:
        try:
            exec(code, self._make_globals(tables))
            plt.close("all")
            return True, None
        except Exception:
            return False, traceback.format_exc()

    def _render(self, code: str, tables):
        """exec 代码 + 导出 SVG，并**保留 fig 引用**。

        返回值 (ok, svg, fig, err)。保留 fig 是因为 injected 组要从图里反解
        真实绘图数据（a11y.extract.figure_to_csv），一旦 plt.close 就拿不到了。
        调用方负责最后 plt.close("all")。
        """
        try:
            plt.close("all")
            exec(code, self._make_globals(tables))
            fig = plt.gcf()
            if not fig.get_axes():
                return False, "", None, "No axes were drawn."
            buf = io.StringIO()
            fig.savefig(buf, format="svg", bbox_inches="tight")
            return True, buf.getvalue(), fig, None
        except Exception:
            return False, "", None, traceback.format_exc()

    # ---------------- 提示词构建 ----------------
    def _official_pre_code(self) -> str:
        library = self.config.get("library", "matplotlib")
        imports = "import seaborn as sns\n" if library == "seaborn" else ""
        return OFFICIAL_PRE_CODE.format(import_statements=imports)

    def _build_official(self, nl_query, tables):
        """严格对齐官方 chat2vis.py：只喂 tables[0]，dataframe 名字固定叫 df。"""
        columns, description = describe_table_official(tables[0])
        return OFFICIAL_TEMPLATE.format(
            columns=columns,
            columns_description=description,
            library=self.config.get("library", "matplotlib"),
            nl_query=nl_query,
            pre_code=self._official_pre_code(),
        )

    @staticmethod
    def _postprocess_official(code: str | None) -> str | None:
        """官方的两步后处理：删掉含 data_file.csv 的行，末尾补 plt.show()。"""
        if code is None:
            return None
        codes = [row for row in code.split("\n") if "data_file.csv" not in row]
        code = "\n".join(codes)
        if "plt.show()" not in code and ("plt." in code or "fig." in code):
            code += "\nplt.show()"
        return code

    def _build_ours(self, nl_query, tables, names):
        schema = "\n\n".join(
            f"{describe_table(t, self.nrows)}\n(already loaded as `{n}`)"
            for n, t in zip(names, tables)
        )
        return (
            SYSTEM_PROMPT.format(names=", ".join(names))
            + "\n\nAvailable tables:\n" + schema
            + f"\n\nRequest: {nl_query}\n"
        )

    # ---------------- 调用模型 ----------------
    def _invoke(self, prompt, tries: int = 6):
        """带退避重试的 invoke。

        为什么需要：山大 API 平台（One-API 类网关）会间歇性返回 `404 page not found`
        ——不是模型名错，是网关/后端抽风。langchain 自带的 max_retries 顶不住持续抖动，
        一崩就得手动续跑，很浪费。这里在**异常**时才重试，不改 prompt、不影响评分口径。
        """
        delays = [5, 10, 20, 40, 60, 60]
        last_err = None
        for i in range(tries):
            try:
                return self.llm.invoke(prompt)
            except Exception as e:  # noqa: BLE001  网关错误类型太多，一律重试
                last_err = e
                if i == tries - 1:
                    break
                time.sleep(delays[min(i, len(delays) - 1)])
        raise last_err

    # ---------------- Agent 接口 ----------------
    def generate(self, nl_query, tables, config):
        names = [f"df{i}" for i in range(1, len(tables) + 1)]

        # ---- official：一次生成，不自修复，和官方 chat2vis.py 完全一致 ----
        if self.prompt_style == "official":
            base = self._build_official(nl_query, tables)

            # 忠实官方：单次生成，不重试
            if self.max_repair <= 0:
                raw = getattr(self._invoke(base), "content", "")
                body = self._postprocess_official(extract_code(raw)) or ""
                return self._official_pre_code() + "\n" + body, {
                    "tables": tables,
                    "df_names": names,
                }

            # 消融用：官方 prompt 不变，只把自修复打开。
            # 注意可执行的始终是 pre_code + 模型代码，所以试跑也要拼上 pre_code。
            pre = self._official_pre_code()
            prompt, code = base, None
            for _ in range(self.max_repair + 1):
                raw = getattr(self._invoke(prompt), "content", "")
                candidate = self._postprocess_official(extract_code(raw))
                if candidate is None:
                    prompt = base + "\n\n" + NO_BLOCK_PROMPT
                    continue
                full = pre + "\n" + candidate
                ok, err = self._try_run(full, tables)
                if ok:
                    code = candidate
                    break
                prompt = prompt + "\n\n" + REPAIR_PROMPT.format(error=err[:1500])
                code = candidate
            return pre + "\n" + (code or ""), {
                "tables": tables,
                "df_names": names,
            }

        # ---- ours：表头 + 3 行样例，允许自修复 ----
        base = self._build_ours(nl_query, tables, names)
        if self.a11y_prompt:
            base = base + "\n" + A11Y_ADDENDUM

        # 总预算 = 首次生成 1 次 + 代码修复 max_repair 次 + 无障碍修复 a11y_repair 次。
        # 两类修复分开计数：代码错了走 REPAIR_PROMPT（traceback），
        # 代码能跑但 Chartability 不过走 A11Y_REPAIR_PROMPT（失败清单）。
        total = max(1, self.max_repair + (self.a11y_repair if self.a11y_prompt else 0) + 1)
        prompt, code = base, None
        fields: dict = {}
        code_used = a11y_used = 0
        best: tuple[str, dict] | None = None  # 最近一次「代码能跑」的版本

        for _ in range(total):
            resp = self._invoke(prompt)
            raw = getattr(resp, "content", str(resp))
            candidate = extract_code(raw)
            if candidate is None:
                prompt = prompt + "\n\n" + NO_BLOCK_PROMPT
                continue
            cand_fields = parse_a11y_fields(raw) if self.a11y_prompt else {}

            ok, svg, _fig, err = self._render(candidate, tables)
            plt.close("all")
            if not ok:
                code = candidate  # 保留最后一版，交给 evaluator 报出真实执行错误
                if code_used < self.max_repair:
                    prompt = prompt + "\n\n" + REPAIR_PROMPT.format(error=(err or "")[:1500])
                    code_used += 1
                    continue
                # 代码修复预算用尽：不能再耗下去，否则总预算会被代码错误吃光，
                # 无障碍修复永远轮不到（这正是要分两个计数器的原因）
                break

            # 代码跑通了
            best = (candidate, cand_fields)
            if not self.a11y_prompt or self.a11y_repair <= 0:
                break

            # 无障碍反馈：先把三件套搬进 SVG（P2 判的就是这个），再跑检查器
            svg_a11y = self._with_metadata(svg, cand_fields)
            results = run_chartability(svg_a11y, cand_fields)
            fails = [r for r in results if r.status == "fail"]
            if fails and a11y_used < self.a11y_repair:
                if not has_any_field(cand_fields):
                    # 模型压根没给元数据块：直接点名要它补，比列一长串失败项更管用
                    feedback = A11Y_MISSING_PROMPT
                else:
                    feedback = A11Y_REPAIR_PROMPT.format(
                        n=len(fails), fails=format_failures(fails))
                prompt = prompt + "\n\n" + feedback
                a11y_used += 1
                continue
            break

        if best is not None:
            code, fields = best
        return code, {
            "tables": tables,
            "df_names": names,
            "a11y": fields,
            "a11y_stats": {"code_repairs": code_used, "a11y_repairs": a11y_used},
        }

    @staticmethod
    def _with_metadata(svg: str, fields: dict) -> str:
        """把三件套搬进 SVG：<title> ← title，<desc> ← readout + takeaway。

        这一步**两组都做**（generated / injected 一视同仁），否则 P2 测的是
        "matplotlib 会不会写 ARIA"而不是模型。区别只在内容来源：
        generated 用模型写的文本，injected 用兜底规则生成的文本。
        """
        desc = " ".join(
            x for x in (fields.get("readout_note", ""), fields.get("takeaway", "")) if x
        ).strip()
        return inject_svg_metadata(svg, title=fields.get("title", ""), desc=desc)

    def execute(self, code, context, log_name=None):
        ok, svg, _fig, err = self._render(code, context["tables"])
        if not ok:
            plt.close("all")
            return ChartExecutionResult(
                status=False,
                error_msg=(err or "").strip() or "No axes were drawn.",
            )
        # 三件套存在就把它们搬进 SVG，保证落盘的图和检查器看到的是同一份
        if context.get("a11y"):
            svg = self._with_metadata(svg, context["a11y"])
        plt.close("all")
        return ChartExecutionResult(status=True, svg_string=svg)
