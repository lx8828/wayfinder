"""ReAct 引擎测试（design.md 第 7 节 + 验收项）。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

import first_agent.evals as evals_pkg
from first_agent.core.client import LLMClient
from first_agent.evals import load_cases, run_case
from first_agent.evals.adapters import react_engine
from first_agent.evals.scripted import ScriptedLLM, ToolCallSpec, fail, text, tool, tools
from first_agent.providers import FakeWeatherProvider
from first_agent.runtime import ReactEngine
from first_agent.tools.builtin import make_time_tool, make_weather_tool
from first_agent.tools.registry import ToolRegistry

FIXED_CLOCK = lambda: datetime(2026, 10, 10, 8, 30, tzinfo=timezone.utc)  # noqa: E731


def build_engine(script, *, max_steps=5, tracer=None, provider=None):
    provider = provider or FakeWeatherProvider()
    registry = ToolRegistry()
    registry.register(make_weather_tool(provider, today=lambda: "2026-10-10"))
    registry.register(make_time_tool(clock=FIXED_CLOCK))
    engine = ReactEngine(
        LLMClient(ScriptedLLM(script)), registry, max_steps=max_steps, tracer=tracer
    )
    return engine, provider


# -- 主循环 ----------------------------------------------------------------


def test_single_tool_then_final_answer():
    engine, _ = build_engine(
        [tool("get_weather", {"city": "北京"}, thought="先查天气"), text("北京今天晴，28°C")]
    )

    result = engine.run("北京今天天气")

    assert result.status == "success"
    assert result.answer == "北京今天晴，28°C"
    assert result.tool_calls == ["get_weather"]
    assert result.thoughts == ["先查天气"]
    assert result.steps == 2


def test_thought_is_optional():
    engine, _ = build_engine([tool("get_weather", {"city": "北京"}), text("晴")])

    result = engine.run("天气")

    assert result.thoughts == []  # 缺失不报错（方案 A）
    assert result.status == "success"


def test_parallel_tool_calls_pair_by_call_id():
    engine, _ = build_engine(
        [
            tools(
                [
                    ToolCallSpec("get_weather", {"city": "北京"}),
                    ToolCallSpec("get_current_time", {}),
                ],
                thought="并行查",
            ),
            text("北京晴；现在 16:30"),
        ]
    )

    result = engine.run("北京天气和现在几点")

    assert result.tool_calls == ["get_weather", "get_current_time"]
    assert [item.call_id for item in result.tool_results] == ["call_1_0", "call_1_1"]
    assert all(item.ok for item in result.tool_results)


def test_unknown_tool_becomes_structured_error_and_loop_continues():
    engine, _ = build_engine([tool("get_traffic", {"city": "北京"}), text("暂时查不到路况")])

    result = engine.run("路况")

    assert result.status == "success"
    assert result.tool_results[0].error_code == "unknown_tool"
    assert result.answer == "暂时查不到路况"


def test_invalid_arguments_never_reach_provider():
    engine, provider = build_engine([tool("get_weather", {}), text("请提供城市")])

    result = engine.run("天气")

    assert result.tool_results[0].error_code == "invalid_arguments"
    assert provider.calls == []


def test_provider_failure_is_surfaced_to_model():
    provider = FakeWeatherProvider(failures={"北京": "unavailable"})
    engine, _ = build_engine(
        [tool("get_weather", {"city": "北京"}), text("暂时查不到北京天气")], provider=provider
    )

    result = engine.run("北京天气")

    assert result.tool_results[0].error_code == "unavailable"
    assert result.answer == "暂时查不到北京天气"


def test_consecutive_identical_call_is_blocked():
    engine, provider = build_engine(
        [
            tool("get_weather", {"city": "北京"}),
            tool("get_weather", {"city": "北京"}),  # 完全相同 → 拦截
            text("北京晴"),
        ]
    )

    result = engine.run("北京天气")

    assert result.tool_calls == ["get_weather"]  # 只执行一次
    assert len(provider.calls) == 1
    assert [item.error_code for item in result.tool_results] == [None, "duplicate_call"]
    assert result.answer == "北京晴"


def test_max_steps_exhausted_returns_explanatory_answer():
    script = [tool("get_weather", {"city": f"城市{index}"}) for index in range(4)]
    engine, _ = build_engine(script, max_steps=2)

    result = engine.run("一直查")

    assert result.status == "max_steps"
    assert "更多步骤" in result.answer
    assert result.steps == 2


def test_llm_error_sets_error_status():
    engine, _ = build_engine([fail(TimeoutError("boom"))])

    result = engine.run("天气")

    assert result.status == "error"
    assert "boom" in result.error


def test_assistant_message_precedes_tool_message():
    sdk = ScriptedLLM([tool("get_weather", {"city": "北京"}), text("晴")])
    registry = ToolRegistry()
    registry.register(make_weather_tool(FakeWeatherProvider(), today=lambda: "2026-10-10"))
    ReactEngine(LLMClient(sdk), registry).run("北京天气")

    second_request = sdk.calls[1]["messages"]
    assert [message["role"] for message in second_request] == ["system", "user", "assistant", "tool"]

    assistant = second_request[2]
    assert assistant["tool_calls"][0]["id"] == "call_1_0"
    assert assistant["tool_calls"][0]["function"]["name"] == "get_weather"
    assert second_request[3]["tool_call_id"] == "call_1_0"


def test_tracer_receives_thought_tool_and_answer_events():
    class Recorder:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        def log(self, kind, **payload):
            self.events.append((kind, payload))

    recorder = Recorder()
    engine, _ = build_engine(
        [tool("get_weather", {"city": "北京"}, thought="想一下"), text("晴")], tracer=recorder
    )
    engine.run("北京天气")

    kinds = [kind for kind, _ in recorder.events]
    assert "react_thought" in kinds
    assert "tool_call" in kinds
    assert "tool_result" in kinds
    assert "react_answer" in kinds

    thought_event = next(payload for kind, payload in recorder.events if kind == "react_thought")
    assert thought_event["thought"] == "想一下"


def test_max_steps_must_be_positive():
    registry = ToolRegistry()
    with pytest.raises(ValueError):
        ReactEngine(LLMClient(ScriptedLLM([])), registry, max_steps=0)


# -- 接入评测护栏 ----------------------------------------------------------


def test_react_engine_passes_seed_case_through_harness():
    cases = {
        case.case_id: case
        for case in load_cases(Path(evals_pkg.__file__).parent / "cases.jsonl")
    }
    engine, _ = build_engine(
        [tool("get_weather", {"city": "北京"}, thought="查天气"), text("北京今天晴，28°C")]
    )

    check = run_case(cases["react_001"], react_engine(engine))

    assert check.passed is True
    assert check.outcome.tool_calls == ["get_weather"]
    assert check.outcome.steps == 2


def test_react_engine_passes_no_tool_case_through_harness():
    cases = {
        case.case_id: case
        for case in load_cases(Path(evals_pkg.__file__).parent / "cases.jsonl")
    }
    engine, _ = build_engine([text("你好！需要查天气或时间吗？")])

    check = run_case(cases["react_003"], react_engine(engine))

    assert check.passed is True
    assert check.outcome.tool_calls == []
