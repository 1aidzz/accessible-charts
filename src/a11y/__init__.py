"""a11y 无障碍检查器包。

用法：`from a11y import run_chartability, compute_cpr`（src 目录要在 sys.path 上）。

注意：这里必须用相对导入（`.checker`）。写 `from checker import` 只有当
`src/a11y` 本身被塞进 sys.path 时才成立，从脚本里 `import a11y` 会直接报
ModuleNotFoundError。tests/ 里为了直接 import 单个模块而手动加了那个路径，
那是测试的特例，不是包的正常入口。
"""
from .checker import (
    CRITICAL_IDS,
    CPR,
    CheckResult,
    compute_cpr,
    run_chartability,
)
from .contract import (
    A11Y_CODE_RULES,
    A11Y_OUTPUT_SPEC,
    A11Y_REPAIR_PROMPT,
    build_injected,
    csv_quality,
    format_failures,
    has_any_field,
    inject_svg_metadata,
    parse_a11y_fields,
)
from .extract import figure_to_csv, table_match_rate

__all__ = [
    "CheckResult", "CPR", "CRITICAL_IDS", "run_chartability", "compute_cpr",
    "A11Y_CODE_RULES", "A11Y_OUTPUT_SPEC", "A11Y_REPAIR_PROMPT",
    "build_injected", "csv_quality", "format_failures", "has_any_field",
    "inject_svg_metadata", "parse_a11y_fields",
    "figure_to_csv", "table_match_rate",
]
