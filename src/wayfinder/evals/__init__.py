"""离线评测：用例、脚本化假模型、执行骨架与指标。

S2 提供可注入引擎的骨架；用例规模在 Phase 4 扩到 30～40 条（design.md 16.1）。
"""

from wayfinder.evals.case import (
    HARD_MAX_STEPS,
    Assertion,
    EvalCase,
    OfflineFixture,
    effective_max_steps,
    eval_user_id,
    load_cases,
)
from wayfinder.evals.metrics import (
    METRIC_SPECS,
    MetricDelta,
    compare_metrics,
    compute_metrics,
    format_diff,
    gate_failures,
    load_metrics,
    save_metrics,
)
from wayfinder.evals.runner import (
    CaseCheck,
    EngineFactory,
    RunOutcome,
    run_case,
    run_cases,
    run_cases_isolated,
)
from wayfinder.evals.suite import (
    GateResult,
    SuiteRun,
    build_metadata,
    evaluate_gate,
    failure_report,
    load_baseline,
    run_suite,
    save_baseline,
    write_outputs,
)
from wayfinder.evals.scripted import (
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
    "OfflineFixture",
    "load_cases",
    "effective_max_steps",
    "eval_user_id",
    "HARD_MAX_STEPS",
    # suite
    "SuiteRun",
    "GateResult",
    "build_metadata",
    "evaluate_gate",
    "failure_report",
    "load_baseline",
    "run_suite",
    "save_baseline",
    "write_outputs",
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
    "EngineFactory",
    "run_case",
    "run_cases",
    "run_cases_isolated",
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
