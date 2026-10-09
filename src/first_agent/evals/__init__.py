"""离线评测：用例、脚本化假模型、执行骨架与指标。

S2 提供可注入引擎的骨架；用例规模在 Phase 4 扩到 30～40 条（design.md 16.1）。
"""

from first_agent.evals.case import (
    HARD_MAX_STEPS,
    Assertion,
    EvalCase,
    effective_max_steps,
    load_cases,
)
from first_agent.evals.metrics import (
    METRIC_SPECS,
    MetricDelta,
    compare_metrics,
    compute_metrics,
    format_diff,
    gate_failures,
    load_metrics,
    save_metrics,
)
from first_agent.evals.runner import CaseCheck, RunOutcome, run_case, run_cases
from first_agent.evals.scripted import (
    ScriptedLLM,
    ToolCallSpec,
    fail,
    json_obj,
    json_raw,
    text,
    tool,
    tools,
)

__all__ = [
    # case
    "EvalCase",
    "Assertion",
    "load_cases",
    "effective_max_steps",
    "HARD_MAX_STEPS",
    # scripted
    "ScriptedLLM",
    "ToolCallSpec",
    "text",
    "tool",
    "tools",
    "json_obj",
    "json_raw",
    "fail",
    # runner
    "RunOutcome",
    "CaseCheck",
    "run_case",
    "run_cases",
    # metrics
    "METRIC_SPECS",
    "MetricDelta",
    "compute_metrics",
    "compare_metrics",
    "gate_failures",
    "format_diff",
    "save_metrics",
    "load_metrics",
]
