"""把真实引擎的产出适配为评测结果（design.md 16.3）。

方向：``evals → runtime``（评测依赖被测对象），runtime 不反向依赖 evals。
"""

from __future__ import annotations

from typing import Any

from first_agent.evals.runner import RunOutcome


def to_run_outcome(result: Any) -> RunOutcome:
    """把 ``ReactResult``（或结构相同的对象）转成 ``RunOutcome``。"""

    return RunOutcome(
        answer=getattr(result, "answer", "") or "",
        tool_calls=list(getattr(result, "tool_calls", []) or []),
        arguments={key: list(value) for key, value in (getattr(result, "arguments", {}) or {}).items()},
        steps=int(getattr(result, "steps", 0) or 0),
        latency_ms=int(getattr(result, "latency_ms", 0) or 0),
        status=getattr(result, "status", "success") or "success",
        error=getattr(result, "error", None),
    )


def react_engine(engine: Any) -> Any:
    """把一个 ``ReactEngine`` 包装成 ``Engine`` 协议：``(case) -> RunOutcome``。"""

    def run(case: Any) -> RunOutcome:
        return to_run_outcome(engine.run(case.input))

    return run
