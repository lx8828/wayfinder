"""计划校验与拓扑执行测试（design.md 8.1 / 8.2）。

对应验收项 ``test_plan_topological_sort`` 与 ``test_plan_reference_requires_dependency``。
"""

from __future__ import annotations

import pytest

from first_agent.runtime import PlanExecutionError, resolve_arguments, topological_order, validate_plan
from first_agent.schemas.plan import Plan, PlanStep
from tests.plan_helpers import make_registry


def problems_of(plan: Plan, *, max_steps: int = 5) -> dict[str, list[str]]:
    collected: dict[str, list[str]] = {}
    for problem in validate_plan(plan, make_registry(), max_steps=max_steps):
        collected.setdefault(problem.kind, []).append(problem.detail)
    return collected


# -- 校验 ------------------------------------------------------------------


def test_valid_plan_has_no_problems():
    plan = Plan(
        steps=[
            PlanStep(id=0, tool="resolve_datetime", arguments={"expression": "明天"}),
            PlanStep(
                id=1,
                tool="get_weather",
                arguments={"city": "重庆", "date": "$steps.0.date"},
                depends_on=[0],
            ),
        ]
    )

    assert problems_of(plan) == {}


def test_duplicate_ids_detected():
    plan = Plan(steps=[PlanStep(id=0, tool="get_current_time"), PlanStep(id=0, tool="get_current_time")])

    assert "duplicate_id" in problems_of(plan)


def test_unknown_tool_detected():
    plan = Plan(steps=[PlanStep(id=0, tool="get_traffic")])

    assert "unknown_tool" in problems_of(plan)


def test_missing_dependency_detected():
    plan = Plan(steps=[PlanStep(id=0, tool="get_current_time", depends_on=[7])])

    assert "missing_dependency" in problems_of(plan)


def test_reference_requires_depends_on():
    plan = Plan(
        steps=[
            PlanStep(id=0, tool="resolve_datetime", arguments={"expression": "明天"}),
            # 引用了 step 0，却没写 depends_on
            PlanStep(id=1, tool="get_weather", arguments={"city": "重庆", "date": "$steps.0.date"}),
        ]
    )

    assert "missing_dependency" in problems_of(plan)


def test_reference_to_unknown_step_detected():
    plan = Plan(
        steps=[
            PlanStep(
                id=0,
                tool="get_weather",
                arguments={"city": "重庆", "date": "$steps.5.date"},
                depends_on=[5],
            )
        ]
    )

    problems = problems_of(plan)

    assert "bad_reference" in problems
    assert "missing_dependency" in problems


def test_reference_to_unknown_field_detected():
    plan = Plan(
        steps=[
            PlanStep(id=0, tool="resolve_datetime", arguments={"expression": "明天"}),
            PlanStep(
                id=1,
                tool="get_weather",
                arguments={"city": "重庆", "date": "$steps.0.temperature"},
                depends_on=[0],
            ),
        ]
    )

    assert "unknown_field" in problems_of(plan)


def test_malformed_reference_detected():
    plan = Plan(
        steps=[
            PlanStep(id=0, tool="resolve_datetime", arguments={"expression": "明天"}),
            PlanStep(
                id=1,
                tool="get_weather",
                arguments={"city": "重庆", "date": "$steps.abc.date"},
                depends_on=[0],
            ),
        ]
    )

    assert "bad_reference" in problems_of(plan)


def test_cycle_detected():
    plan = Plan(
        steps=[
            PlanStep(
                id=0,
                tool="get_weather",
                arguments={"city": "重庆", "date": "$steps.1.date"},
                depends_on=[1],
            ),
            PlanStep(id=1, tool="resolve_datetime", arguments={"expression": "明天"}, depends_on=[0]),
        ]
    )

    assert "cycle" in problems_of(plan)


def test_self_dependency_detected():
    plan = Plan(steps=[PlanStep(id=0, tool="get_current_time", depends_on=[0])])

    assert "cycle" in problems_of(plan)


def test_too_many_steps_detected():
    plan = Plan(
        steps=[PlanStep(id=index, tool="get_current_time") for index in range(4)]
    )

    assert "too_many_steps" in problems_of(plan, max_steps=3)


# -- 拓扑排序 --------------------------------------------------------------


def test_topological_order_is_dependency_first():
    plan = Plan(
        steps=[
            # 故意把依赖者写在前面
            PlanStep(
                id=1,
                tool="get_weather",
                arguments={"city": "重庆", "date": "$steps.0.date"},
                depends_on=[0],
            ),
            PlanStep(id=0, tool="resolve_datetime", arguments={"expression": "明天"}),
        ]
    )

    assert [step.id for step in topological_order(plan)] == [0, 1]


def test_topological_order_is_deterministic_for_independent_steps():
    plan = Plan(
        steps=[PlanStep(id=index, tool="get_current_time") for index in (2, 0, 1)]
    )

    assert [step.id for step in topological_order(plan)] == [0, 1, 2]


def test_topological_order_raises_on_cycle():
    plan = Plan(
        steps=[
            PlanStep(id=0, tool="get_current_time", depends_on=[1]),
            PlanStep(id=1, tool="get_current_time", depends_on=[0]),
        ]
    )

    with pytest.raises(PlanExecutionError, match="成环"):
        topological_order(plan)


# -- 引用解析 --------------------------------------------------------------


def test_resolve_arguments_replaces_references():
    step = PlanStep(
        id=1,
        tool="get_weather",
        arguments={"city": "重庆", "date": "$steps.0.date"},
        depends_on=[0],
    )

    resolved = resolve_arguments(step, {0: {"date": "2026-10-11"}})

    assert resolved == {"city": "重庆", "date": "2026-10-11"}


def test_resolve_arguments_handles_nested_structures():
    step = PlanStep(
        id=1,
        tool="x",
        arguments={"a": {"b": "$steps.0.date"}, "c": ["$steps.0.time", "plain"]},
    )

    resolved = resolve_arguments(step, {0: {"date": "2026-10-11", "time": "14:00"}})

    assert resolved == {"a": {"b": "2026-10-11"}, "c": ["14:00", "plain"]}


def test_resolve_arguments_raises_when_source_missing():
    step = PlanStep(id=1, tool="x", arguments={"date": "$steps.0.date"})

    with pytest.raises(PlanExecutionError, match="尚无结果"):
        resolve_arguments(step, {})


def test_resolve_arguments_raises_when_field_missing():
    step = PlanStep(id=1, tool="x", arguments={"date": "$steps.0.nope"})

    with pytest.raises(PlanExecutionError, match="没有字段"):
        resolve_arguments(step, {0: {"date": "2026-10-11"}})
