"""评测适配器测试：用例级隔离、记忆预置与记忆动作贯通（design.md 16.3 / 16.4）。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import wayfinder.evals as evals_pkg
from wayfinder.core.client import LLMClient
from wayfinder.core.db import Database
from wayfinder.core.engine import ToolAgentRuntime
from wayfinder.core.session import SessionResolver, SessionStore
from wayfinder.evals import EvalCase, load_cases, run_case
from wayfinder.evals.adapters import runtime_engine
from wayfinder.evals.scripted import ScriptedLLM, json_obj, json_raw, text
from wayfinder.memory.service import MemoryService
from wayfinder.memory.store import MemoryStore
from wayfinder.providers import FakeWeatherProvider
from wayfinder.runtime import ReactEngine
from wayfinder.tools.builtin import make_time_tool, make_weather_tool
from wayfinder.tools.registry import ToolRegistry

FIXED_CLOCK = lambda: datetime(2026, 10, 10, 8, 30, tzinfo=timezone.utc)  # noqa: E731
CASES_PATH = Path(evals_pkg.__file__).parent / "cases.jsonl"

EMPTY_EXTRACTION = json_obj({"memories": []})


def make_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(make_weather_tool(FakeWeatherProvider(), today=lambda: "2026-10-10"))
    registry.register(make_time_tool(clock=FIXED_CLOCK))
    return registry


def build(script, *, with_memory: bool = True):
    sdk = ScriptedLLM(script)
    db = Database(":memory:")
    memories = MemoryStore(db)
    runtime = ToolAgentRuntime(
        engine=ReactEngine(LLMClient(sdk), make_registry()),
        sessions=SessionStore(db),
        memories=memories,
        memory_service=MemoryService(LLMClient(sdk), memories) if with_memory else None,
        resolver=SessionResolver(),
    )
    return runtime, sdk, memories


def test_initial_memory_is_seeded_under_the_case_user():
    runtime, sdk, _ = build([text("多云"), EMPTY_EXTRACTION])
    case = EvalCase(
        case_id="c1", mode="react", input="我这里天气", initial_memory={"home_city": "重庆"}
    )

    runtime_engine(runtime)(case)

    prompt = sdk.calls[0]["messages"][0]["content"]
    assert "home_city：重庆" in prompt


def test_seed_memory_can_be_disabled():
    runtime, sdk, _ = build([text("嗨"), EMPTY_EXTRACTION])
    case = EvalCase(
        case_id="c1", mode="react", input="你好", initial_memory={"home_city": "重庆"}
    )

    runtime_engine(runtime, seed_memory=False)(case)

    assert "home_city" not in sdk.calls[0]["messages"][0]["content"]


def test_cases_are_isolated_by_case_id():
    """design 16.4：不允许在用例之间共享记忆。"""

    runtime, sdk, _ = build([text("a"), EMPTY_EXTRACTION, text("b"), EMPTY_EXTRACTION])
    engine = runtime_engine(runtime)
    case_a = EvalCase(
        case_id="a", mode="react", input="我这里天气", initial_memory={"home_city": "重庆"}
    )
    case_b = EvalCase(
        case_id="b", mode="react", input="我这里天气", initial_memory={"home_city": "北京"}
    )

    engine(case_a)
    engine(case_b)

    prompt_a = sdk.calls[0]["messages"][0]["content"]
    prompt_b = sdk.calls[2]["messages"][0]["content"]
    assert "home_city：重庆" in prompt_a
    assert "home_city：北京" not in prompt_a
    assert "home_city：北京" in prompt_b
    assert "home_city：重庆" not in prompt_b


def test_memory_actions_flow_into_run_outcome():
    runtime, _, _ = build(
        [
            text("好的"),
            json_obj(
                {"memories": [{"key": "home_city", "value": "重庆", "category": "user_identity"}]}
            ),
        ]
    )
    case = EvalCase(case_id="c1", mode="react", input="我住在重庆")

    outcome = runtime_engine(runtime)(case)

    assert outcome.memory_actions == {"home_city": "add"}
    assert outcome.structured_output_ok is True


def test_structured_output_ok_is_false_when_extraction_fails():
    runtime, _, _ = build([text("好的"), json_raw("bad"), json_raw("still bad")])
    case = EvalCase(case_id="c1", mode="react", input="我叫张三")

    outcome = runtime_engine(runtime)(case)

    assert outcome.structured_output_ok is False
    assert outcome.memory_actions == {}


def test_engine_can_run_without_memory_service():
    runtime, _, _ = build([text("好的")], with_memory=False)
    case = EvalCase(case_id="c1", mode="react", input="你好")

    outcome = runtime_engine(runtime)(case)

    assert outcome.answer == "好的"
    assert outcome.memory_actions == {}


def test_case_memory_add_028_passes_through_the_harness():
    """用例 memory_add_028：两个 key 都是 add（expected_memory_delta 精确匹配）。"""

    cases = {case.case_id: case for case in load_cases(CASES_PATH)}
    runtime, _, memories = build(
        [
            text("好的，记住了"),
            json_obj(
                {
                    "memories": [
                        {"key": "user_name", "value": "张三", "category": "user_identity"},
                        {"key": "home_city", "value": "重庆", "category": "user_identity"},
                    ]
                }
            ),
        ]
    )

    check = run_case(cases["memory_add_028"], runtime_engine(runtime))

    assert check.error is None
    assert check.outcome.memory_actions == {"user_name": "add", "home_city": "add"}
    assert check.passed is True
    assert memories.values("case:memory_add_028") == {"user_name": "张三", "home_city": "重庆"}
