"""计划类指标：Tool Set / Precedence / Plan Validity / Plan Success。

设计要点（用户口径）：

- 把"顺序对不对"拆成**可独立判定**的维度，而不是只用一个"序列完全相等"：
  - ``Tool Set Accuracy``：必要工具是否都调了（只召回）
  - ``Precedence Accuracy``：**只检查声明的关键先后约束**，不追求唯一序列
  - ``Plan Validity Rate``：集合正确 + DAG 合法 + 无失败工具 + 无多余调用
  - ``Plan Success Rate``：计划合法**且**答案通过断言
- ``Tool Sequence Accuracy`` 仍是主指标之一，但不再是唯一。
"""

from __future__ import annotations

import json

import pytest

from first_agent.core.client import LLMClient
from first_agent.evals.adapters import to_run_outcome
from first_agent.evals.case import (
    EvalCase,
    load_cases,
    precedence_matches,
    required_tools,
    tool_recall_ok,
)
from first_agent.evals.metrics import compute_metrics
from first_agent.evals.runner import (
    CaseCheck,
    RunOutcome,
    plan_success_ok,
    plan_validity_ok,
    run_case,
)
from first_agent.evals.scripted import ScriptedLLM, json_obj, text, tool
from first_agent.runtime import PlanExecuteEngine, ReactEngine
from tests.plan_helpers import build_engine, make_registry, user_messages

# -- 辅助 ------------------------------------------------------------------


def make_case(**overrides) -> EvalCase:
    base: dict = {"case_id": "c1", "mode": "plan", "input": "明天天气"}
    base.update(overrides)
    return EvalCase(**base)


def check_for(case: EvalCase, **outcome: object) -> CaseCheck:
    def engine(_case: EvalCase) -> RunOutcome:
        return RunOutcome(**outcome)  # type: ignore[arg-type]

    return run_case(case, engine)


# -- Tool Set Accuracy（召回） --------------------------------------------


def test_tool_set_accuracy_only_checks_recall():
    """多调了工具不影响召回，但会让「集合相等」失败——两者是不同问题。"""

    case = make_case(expected_tool_set=["a", "b"])

    assert tool_recall_ok(case, ["a", "b", "c"]) is True

    check = check_for(case, tool_calls=["a", "b", "c"], steps=3)
    assert check.checks["tool_recall"] is True
    assert check.checks["tool_set"] is False


def test_tool_set_accuracy_fails_when_a_required_tool_is_missing():
    case = make_case(expected_tool_set=["a", "b"])

    assert tool_recall_ok(case, ["a"]) is False
    assert tool_recall_ok(case, []) is False


def test_tool_set_accuracy_not_applicable_without_declaration():
    case = make_case()

    assert required_tools(case) is None
    assert tool_recall_ok(case, ["whatever"]) is None


def test_declared_empty_tool_set_means_nothing_is_required():
    case = make_case(expected_tool_set=[])

    assert required_tools(case) == set()
    assert tool_recall_ok(case, []) is True


# -- Precedence Accuracy ---------------------------------------------------


def test_precedence_accepts_the_required_order():
    case = make_case(precedence=[["resolve_datetime", "get_weather"]])

    assert precedence_matches(case, ["resolve_datetime", "get_weather"]) is True


def test_precedence_detects_reversed_order():
    case = make_case(precedence=[["resolve_datetime", "get_weather"]])

    assert precedence_matches(case, ["get_weather", "resolve_datetime"]) is False


def test_precedence_detects_missing_tool():
    case = make_case(precedence=[["resolve_datetime", "get_weather"]])

    assert precedence_matches(case, ["resolve_datetime"]) is False


def test_precedence_ignores_unrelated_order():
    """只约束声明的先后——其它工具的插入与顺序都不该被判错（ReAct 顺序本来不唯一）。"""

    case = make_case(precedence=[["resolve_datetime", "get_weather"]])

    assert precedence_matches(case, ["resolve_datetime", "get_current_time", "get_weather"]) is True
    assert precedence_matches(case, ["get_current_time", "resolve_datetime", "get_weather"]) is True


def test_precedence_not_applicable_without_constraints():
    assert precedence_matches(make_case(), ["a", "b"]) is None


@pytest.mark.parametrize(
    "bad",
    [[["a", "b", "c"]], [["a", "a"]]],
)
def test_precedence_rejects_malformed_constraints(bad):
    case = make_case(precedence=bad)

    with pytest.raises(ValueError, match="precedence"):
        precedence_matches(case, ["a", "b"])


# -- Plan Validity Rate ----------------------------------------------------


def test_plan_validity_requires_all_four_conditions():
    case = make_case(expected_tool_sequence=["a", "b"])

    ok = check_for(case, tool_calls=["a", "b"], plan_valid=True, steps=1)
    assert ok.checks["plan_validity"] is True

    # 1) 工具序列不对
    wrong_order = check_for(case, tool_calls=["b", "a"], plan_valid=True, steps=1)
    assert wrong_order.checks["plan_validity"] is False
    # 2) 依赖 DAG 不合法
    invalid_plan = check_for(case, tool_calls=["a", "b"], plan_valid=False, steps=1)
    assert invalid_plan.checks["plan_validity"] is False
    # 3) 有失败工具
    failed = check_for(
        case,
        tool_calls=["a", "b"],
        plan_valid=True,
        steps=1,
        tool_errors=[{"tool": "a", "error_code": "timeout"}],
    )
    assert failed.checks["plan_validity"] is False
    # 4) 有多余调用
    extra = check_for(case, tool_calls=["a", "b", "c"], plan_valid=True, steps=1)
    assert extra.checks["plan_validity"] is False


def test_plan_validity_not_applicable_for_react_or_missing_signal():
    react = check_for(make_case(mode="react", expected_tool_set=["a"]), tool_calls=["a"])
    assert react.checks["plan_validity"] is None

    # 引擎没上报计划合法性 → 不臆断
    unknown = check_for(make_case(expected_tool_sequence=["a"]), tool_calls=["a"])
    assert unknown.checks["plan_validity"] is None


def test_plan_validity_skipped_for_designed_failures():
    """故意注入工具失败的用例不参与判定，否则衡量的是夹具而不是 Runtime。"""

    case = make_case(
        expected_tool_sequence=["a"], expect_tool_errors=True, expected_status="success"
    )

    check = check_for(
        case,
        tool_calls=["a"],
        plan_valid=True,
        tool_errors=[{"tool": "a", "error_code": "unavailable"}],
    )

    assert check.checks["plan_validity"] is None
    assert check.checks["tool_ok"] is None


# -- Plan Success Rate -----------------------------------------------------


def test_plan_success_requires_validity_and_assertions():
    case = make_case(expected_tool_sequence=["a"], assertions=[{"type": "contains", "value": "晴"}])

    passed = check_for(case, tool_calls=["a"], plan_valid=True, answer="明天晴")
    assert passed.checks["plan_success"] is True

    bad_answer = check_for(case, tool_calls=["a"], plan_valid=True, answer="不知道")
    assert bad_answer.checks["plan_success"] is False

    invalid = check_for(case, tool_calls=["a"], plan_valid=False, answer="明天晴")
    assert invalid.checks["plan_success"] is False


def test_plan_success_without_assertions_is_not_penalised():
    case = make_case(expected_tool_sequence=["a"])

    check = check_for(case, tool_calls=["a"], plan_valid=True, answer="随便")

    assert check.checks["plan_success"] is True


def test_plan_success_helper_returns_none_without_validity():
    assert plan_success_ok(make_case(), {"plan_validity": None}, True) is None
    assert plan_validity_ok(make_case(mode="react"), RunOutcome(), {}) is None


# -- 指标聚合 --------------------------------------------------------------


def test_new_dimensions_appear_in_metrics():
    valid = check_for(
        make_case(case_id="good", expected_tool_sequence=["resolve_datetime", "get_weather"],
                  precedence=[["resolve_datetime", "get_weather"]]),
        tool_calls=["resolve_datetime", "get_weather"],
        plan_valid=True,
        steps=2,
    )

    metrics = compute_metrics([valid])

    assert metrics["Tool Set Accuracy"] == 100.0
    assert metrics["Precedence Accuracy"] == 100.0
    assert metrics["Plan Validity Rate"] == 100.0
    assert metrics["Plan Success Rate"] == 100.0


def test_metrics_drop_when_precedence_is_violated_but_names_still_match():
    """工具名集合完全正确、只错在先后 → 只有 Precedence/Sequence 掉，召回不掉。"""

    case = make_case(
        case_id="reversed",
        expected_tool_sequence=["resolve_datetime", "get_weather"],
        precedence=[["resolve_datetime", "get_weather"]],
    )
    check = check_for(case, tool_calls=["get_weather", "resolve_datetime"], plan_valid=True, steps=2)

    metrics = compute_metrics([check])

    assert metrics["Tool Set Accuracy"] == 100.0  # 必要工具都调了
    # plan 用例不判「集合相等」（那是 react 的口径，由 sequence 负责）
    assert "Tool Selection Accuracy" not in metrics
    assert metrics["Precedence Accuracy"] == 0.0
    assert metrics["Tool Sequence Accuracy"] == 0.0
    assert metrics["Plan Validity Rate"] == 0.0


# -- 真引擎：plan_valid 由引擎上报 ----------------------------------------

VALID_PLAN = {
    "need_info": [],
    "reason": "查时间",
    "steps": [{"id": 0, "tool": "get_current_time", "arguments": {}, "depends_on": []}],
}
BAD_PLAN = {
    "need_info": [],
    "reason": "引用了不存在的工具",
    "steps": [{"id": 0, "tool": "get_traffic", "arguments": {}, "depends_on": []}],
}


def test_engine_reports_valid_plan():
    engine, _ = build_engine([json_obj({}), json_obj(VALID_PLAN), text("16:30")])

    outcome = to_run_outcome(engine.run_messages(user_messages("现在几点")))

    assert outcome.plan_valid is True
    assert outcome.tool_calls == ["get_current_time"]


def test_engine_reports_invalid_plan_even_after_replan():
    engine, _ = build_engine([json_obj({}), json_obj(BAD_PLAN), json_obj(BAD_PLAN)])

    outcome = to_run_outcome(engine.run_messages(user_messages("现在几点")))

    assert outcome.plan_valid is False
    assert outcome.tool_calls == []


def test_engine_reports_need_info_plan_as_valid():
    """需要补充信息的计划结构是合法的：DAG 平凡成立，只是信息不全。"""

    plan = {"need_info": ["destination"], "reason": "缺城市", "steps": []}
    engine, _ = build_engine([json_obj({}), json_obj(plan)])

    outcome = to_run_outcome(engine.run_messages(user_messages("明天天气")))

    assert outcome.status == "need_info"
    assert outcome.plan_valid is True


def test_degraded_run_is_not_counted_as_a_valid_plan():
    """降级到 ReAct 是**好的恢复行为**，但计划本身不合法——两件事必须分开记。"""

    script = [
        json_obj({}),
        json_obj(BAD_PLAN),
        json_obj(BAD_PLAN),
        tool("get_current_time"),
        text("16:30"),
    ]
    sdk = ScriptedLLM(script)
    client = LLMClient(sdk)
    registry = make_registry()
    engine = PlanExecuteEngine(
        client, registry, fallback_engine=ReactEngine(client, registry)
    )

    result = engine.run_messages(user_messages("现在几点"))
    outcome = to_run_outcome(result)

    assert result.degraded_to == "react"  # 恢复成功
    assert outcome.tool_calls == ["get_current_time"]
    assert outcome.plan_valid is False  # 但计划不合法，两者不可混为一谈


# -- 用例加载期的校验 ------------------------------------------------------


def test_load_cases_rejects_malformed_precedence(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text(
        json.dumps(
            {
                "case_id": "bad",
                "mode": "plan",
                "input": "x",
                "precedence": [["a"]],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="precedence"):
        load_cases(path)
