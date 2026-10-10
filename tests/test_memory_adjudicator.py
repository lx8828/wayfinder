"""记忆裁决器测试：只判关系、失败保守（design.md 12.3 / 13）。"""

from __future__ import annotations

import pytest

from wayfinder.core.client import LLMClient
from wayfinder.evals.scripted import ScriptedLLM, json_obj, json_raw
from wayfinder.memory.adjudicator import MemoryAdjudicator


def make_adjudicator(script, **kwargs) -> MemoryAdjudicator:
    return MemoryAdjudicator(LLMClient(ScriptedLLM(script)), **kwargs)


@pytest.mark.parametrize(
    "relation", ["same", "conflict", "complementary", "unsupported"]
)
def test_relation_is_passed_through(relation):
    adjudicator = make_adjudicator([json_obj({"relation": relation, "reason": "理由"})])

    result = adjudicator.adjudicate(key="home_city", old_value="重庆", new_value="杭州")

    assert result.relation == relation
    assert result.reason == "理由"


def test_prompt_carries_old_and_new_value():
    sdk = ScriptedLLM([json_obj({"relation": "conflict", "reason": "搬家"})])
    MemoryAdjudicator(LLMClient(sdk)).adjudicate(
        key="home_city", old_value="重庆", new_value="杭州"
    )

    content = sdk.calls[0]["messages"][-1]["content"]
    assert "home_city" in content
    assert "重庆" in content
    assert "杭州" in content


def test_failure_falls_back_to_unsupported_to_preserve_old_value():
    adjudicator = make_adjudicator([json_raw("不是 JSON"), json_raw("还是不是")])

    result = adjudicator.adjudicate(key="home_city", old_value="重庆", new_value="杭州")

    assert result.relation == "unsupported"  # → decide_action → skip
    assert "保留旧记忆" in result.reason


def test_invalid_relation_value_retries_then_falls_back():
    adjudicator = make_adjudicator(
        [
            json_obj({"relation": "完全不认识的关系"}),
            json_obj({"relation": "still_wrong"}),
        ]
    )

    result = adjudicator.adjudicate(key="home_city", old_value="重庆", new_value="杭州")

    assert result.relation == "unsupported"


def test_tracer_receives_failure_event():
    class Recorder:
        def __init__(self) -> None:
            self.events: list[str] = []

        def log(self, kind, **payload):
            self.events.append(kind)

    recorder = Recorder()
    adjudicator = make_adjudicator([json_raw("bad"), json_raw("bad")], tracer=recorder)

    adjudicator.adjudicate(key="home_city", old_value="重庆", new_value="杭州")

    assert "memory_adjudicate_failed" in recorder.events
