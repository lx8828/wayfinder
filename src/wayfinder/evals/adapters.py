"""把真实引擎的产出适配为评测结果（design.md 16.3 / 16.4）。

方向：``evals → runtime``（评测依赖被测对象），runtime 不反向依赖 evals。
"""

from __future__ import annotations

from typing import Any

from wayfinder.evals.runner import RunOutcome

# case.initial_memory 注入时使用的 category
_SEED_CATEGORY = {
    "home_city": "user_identity",
    "user_name": "user_identity",
    "long_term_plan": "long_term_plan",
    "timezone": "preference",
    "transport_mode": "preference",
    "dietary_preference": "preference",
}


def _tool_errors(result: Any) -> list[dict[str, Any]]:
    """执行失败的调用（含被依赖失败阻塞的步骤）。"""

    return [
        {
            "tool": getattr(item, "tool_name", ""),
            "error_code": getattr(item, "error_code", None),
            "error_message": getattr(item, "error_message", None),
        }
        for item in (getattr(result, "tool_results", None) or [])
        if not getattr(item, "ok", True)
    ]


def _observations(result: Any) -> list[dict[str, Any]]:
    """成功调用的观测数据（供裁判核对回答是否与事实一致）。"""

    return [
        {"tool": getattr(item, "tool_name", ""), "data": getattr(item, "data", None)}
        for item in (getattr(result, "tool_results", None) or [])
        if getattr(item, "ok", False)
    ]


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
        # PlanResult 有该字段；ReactResult 没有 → None（不判定）
        plan_valid=getattr(result, "plan_valid", None),
        tool_errors=_tool_errors(result),
        need_info=list(getattr(result, "need_info", None) or []),
        observations=_observations(result),
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
        from wayfinder.evals.case import eval_user_id
        from wayfinder.schemas.memory import MemoryCandidate

        user_id = eval_user_id(case)
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

        # 按用例声明的 mode 选引擎（react / plan）
        outcome = runtime.chat(
            case.input,
            user_id=user_id,
            session_id=case.case_id,
            mode=getattr(case, "mode", None),
        )
        run_outcome = to_run_outcome(outcome.result)
        run_outcome.memory_actions = dict(outcome.memory_actions)
        run_outcome.structured_output_ok = not outcome.memory_extraction_failed
        return run_outcome

    return run
