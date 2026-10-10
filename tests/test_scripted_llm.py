"""ScriptedLLM 离线护栏测试（design.md 14.4）。"""

from __future__ import annotations

import pytest
from pydantic import BaseModel

from wayfinder.core.client import LLMClient, LLMError
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


class Weather(BaseModel):
    city: str
    temp_c: float


def test_text_step_flows_through_client():
    client = LLMClient(ScriptedLLM([text("北京今天晴")]))
    assert client.chat_text([{"role": "user", "content": "x"}]) == "北京今天晴"


def test_tool_step_normalizes_thought_and_call():
    sdk = ScriptedLLM([tool("get_weather", {"city": "重庆"}, thought="先查天气")])
    turn = LLMClient(sdk).chat_turn(
        [{"role": "user", "content": "x"}], tools=[{"type": "function"}]
    )

    assert turn.thought == "先查天气"
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].tool_name == "get_weather"
    assert turn.tool_calls[0].arguments == {"city": "重庆"}
    assert turn.tool_calls[0].call_id == "call_1_0"
    # 请求确实按原生协议带了 tools
    assert sdk.calls[0]["tools"] == [{"type": "function"}]
    assert sdk.calls[0]["tool_choice"] == "auto"


def test_parallel_tool_step():
    sdk = ScriptedLLM(
        [
            tools(
                [
                    ToolCallSpec("get_weather", {"city": "北京"}),
                    ToolCallSpec("get_current_time", {}),
                ]
            )
        ]
    )
    turn = LLMClient(sdk).chat_turn([{"role": "user", "content": "x"}])

    assert [call.tool_name for call in turn.tool_calls] == ["get_weather", "get_current_time"]
    assert turn.tool_calls[1].call_id == "call_1_1"
    assert turn.thought is None


def test_json_step_drives_chat_json():
    sdk = ScriptedLLM([json_obj({"city": "重庆", "temp_c": 28.5})])
    result = LLMClient(sdk).chat_json([{"role": "user", "content": "x"}], Weather)

    assert result.temp_c == 28.5
    assert sdk.calls[0]["response_format"] == {"type": "json_object"}


def test_json_raw_supports_invalid_json_then_recovery():
    sdk = ScriptedLLM([json_raw("这不是 JSON"), json_obj({"city": "A", "temp_c": 1})])
    result = LLMClient(sdk).chat_json([{"role": "user", "content": "x"}], Weather)

    assert result.city == "A"
    assert len(sdk.calls) == 2


def test_fail_step_surfaces_as_llm_error():
    sdk = ScriptedLLM([fail(TimeoutError("boom"))])
    with pytest.raises(LLMError, match="boom"):
        LLMClient(sdk).chat_text([{"role": "user", "content": "x"}])


def test_script_exhaustion_returns_default_text():
    client = LLMClient(ScriptedLLM([text("第一次")], default_text="脚本用尽"))
    assert client.chat_text([{"role": "user", "content": "x"}]) == "第一次"
    assert client.chat_text([{"role": "user", "content": "x"}]) == "脚本用尽"


def test_strict_mode_flags_script_request_mismatch():
    sdk = ScriptedLLM([text("我是文本")])  # 但接下来请求 JSON mode
    with pytest.raises(AssertionError, match="不匹配"):
        LLMClient(sdk).chat_json([{"role": "user", "content": "x"}], Weather)


def test_non_strict_mode_tolerates_mismatch():
    sdk = ScriptedLLM([text('{"city": "A", "temp_c": 2}')], strict=False)
    assert LLMClient(sdk).chat_json([{"role": "user", "content": "x"}], Weather).city == "A"


def test_calls_are_recorded_in_order():
    sdk = ScriptedLLM([text("一"), text("二")])
    client = LLMClient(sdk)
    client.chat_text([{"role": "user", "content": "a"}])
    client.chat_text([{"role": "user", "content": "b"}])

    assert len(sdk.calls) == 2
    assert sdk.calls[0]["messages"][0]["content"] == "a"
    assert sdk.calls[1]["messages"][0]["content"] == "b"
