"""把真实引擎的产出适配为评测结果（design.md 16.3 / 16.4）。

方向：``evals → runtime``（评测依赖被测对象），runtime 不反向依赖 evals。
"""

from __future__ import annotations

from typing import Any

from first_agent.evals.runner import RunOutcome

# case.initial_memory 注入时使用的 category
_SEED_CATEGORY = {
    "home_city": "user_identity",
    "user_name": "user_identity",
    "long_term_plan": "long_term_plan",
    "timezone": "preference",
    "transport_mode": "preference",
    "dietary_preference": "preference",
}


def to_run_outcome(result: Any) -> RunOutcome:
    """把 ``ReactResult``（或结构相同的对象）转成 ``RunOutcome``。"""

    return RunOutcome(
        answer=getattr(result, "answer", "") or "",
        tool_calls=list(getattr(result, "tool_calls", []) or []),
        arguments={
            key: list(value) for key, value in (getattr(result, "arguments", {}) or {}).items()
        },
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


def runtime_engine(runtime: Any, *, seed_memory: bool = True) -> Any:
    """把 ``ToolAgentRuntime`` 包装成 ``Engine`` 协议：``(case) -> RunOutcome``。

    **跨用例隔离（design.md 16.4）**：每个用例使用独立的 ``user_id``
    （``case:<case_id>``）与 ``session_id``（取 ``case_id``），
    避免记忆与会话在用例之间互相污染。

    ``case.initial_memory`` 会被预置到该用例专属的 ``user_id`` 下。
    """

    def run(case: Any) -> RunOutcome:
        from first_agent.schemas.memory import MemoryCandidate

        user_id = f"case:{case.case_id}"
        if seed_memory:
            for key, value in (getattr(case, "initial_memory", None) or {}).items():
                runtime.memories.apply_candidate(
                    MemoryCandidate(
                        user_id=user_id,
                        key=key,
                        value=value,
                        category=_SEED_CATEGORY.get(key, "preference"),
                        source="eval_seed",
                    )
                )

        outcome = runtime.chat(case.input, user_id=user_id, session_id=case.case_id)
        run_outcome = to_run_outcome(outcome.result)
        run_outcome.memory_actions = dict(outcome.memory_actions)
        run_outcome.structured_output_ok = not outcome.memory_extraction_failed
        return run_outcome

    return run
