"""schemas 契约测试：Plan 引用、WorkingState 合并、记忆关系映射。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from first_agent.schemas import (
    RELATION_TO_ACTION,
    MemoryCandidate,
    MemoryRelation,
    Plan,
    PlanStep,
    WorkingState,
    decide_action,
    iter_references,
    parse_step_reference,
)


# -- Plan / $steps 引用 ----------------------------------------------------


def test_parse_step_reference_valid():
    assert parse_step_reference("$steps.0.date") == (0, "date")
    assert parse_step_reference("$steps.12.resolved_time") == (12, "resolved_time")


def test_parse_step_reference_non_reference_returns_none():
    assert parse_step_reference("重庆") is None
    assert parse_step_reference(None) is None
    assert parse_step_reference(3) is None
    assert parse_step_reference("$step.0.date") is None  # 前缀不符


def test_parse_step_reference_malformed_raises():
    with pytest.raises(ValueError):
        parse_step_reference("$steps.abc.date")
    with pytest.raises(ValueError):
        parse_step_reference("$steps.0.date.extra")


def test_iter_references_walks_nested_structures():
    arguments = {
        "a": "$steps.1.date",
        "nested": {"b": "$steps.2.time"},
        "items": ["$steps.3.city", "plain", {"c": "$steps.4.x"}],
    }
    assert set(iter_references(arguments)) == {
        (1, "date"),
        (2, "time"),
        (3, "city"),
        (4, "x"),
    }


def test_plan_detects_duplicate_ids():
    plan = Plan(
        steps=[
            PlanStep(id=0, tool="a"),
            PlanStep(id=0, tool="b"),
            PlanStep(id=2, tool="c"),
        ]
    )
    assert plan.duplicate_ids() == [0]
    assert plan.step_ids() == {0, 2}


def test_plan_step_rejects_negative_id():
    with pytest.raises(ValidationError):
        PlanStep(id=-1, tool="a")


# -- WorkingState ----------------------------------------------------------


def test_working_state_merge_fills_slots_and_keeps_old():
    base = WorkingState(
        user_id="u1",
        session_id="s1",
        purpose="看电影",
        pending_slots=["目的地"],
    )
    incoming = WorkingState(user_id="u1", session_id="s1", destination="重庆")

    merged = base.merge(incoming)

    assert merged.purpose == "看电影"  # 旧槽位保留
    assert merged.destination == "重庆"  # 新槽位补上
    assert merged.pending_slots == ["目的地"]
    assert base.destination is None  # merge 不改原对象


def test_working_state_merge_keeps_old_when_incoming_empty():
    base = WorkingState(user_id="u1", session_id="s1", destination="重庆")
    incoming = WorkingState(user_id="u1", session_id="s1")
    assert base.merge(incoming).destination == "重庆"


def test_working_state_defaults():
    state = WorkingState(user_id="u1", session_id="s1")
    assert state.pending_slots == []
    assert state.resolved_values == {}
    assert state.destination is None


# -- 记忆契约 --------------------------------------------------------------


def test_memory_relation_rejects_removed_irrelevant():
    with pytest.raises(ValidationError):
        MemoryRelation(relation="irrelevant")


def test_decide_action_is_deterministic_and_total():
    assert decide_action("same") == "keep"
    assert decide_action("conflict") == "update"
    assert decide_action("complementary") == "skip"
    assert decide_action("unsupported") == "skip"
    assert decide_action("unknown-relation") == "skip"  # 不猜测


def test_relation_to_action_covers_all_relations():
    assert set(RELATION_TO_ACTION) == {"same", "conflict", "complementary", "unsupported"}


def test_memory_candidate_requires_english_category():
    candidate = MemoryCandidate(
        user_id="u1", key="home_city", value="重庆", category="user_identity"
    )
    assert candidate.source == "conversation"

    with pytest.raises(ValidationError):
        MemoryCandidate(user_id="u1", key="k", value="v", category="用户身份")
