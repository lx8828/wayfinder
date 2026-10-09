"""``chat_json`` 状态机与原生响应归一化的离线测试（零 API Key）。

对应 design.md 第 12 / 13 节与验收项 ``test_chat_json_retry``。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from first_agent.core.client import JSONValidationError, LLMClient, LLMError


class Weather(BaseModel):
    city: str
    temp_c: float


class ScriptedSDK:
    """模拟 OpenAI 兼容 SDK：按脚本依次返回 ``content``。"""

    def __init__(self, contents) -> None:
        self._contents = list(contents)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        content = self._contents.pop(0) if self._contents else ""
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=None))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2, total_tokens=3),
        )


class TurnSDK:
    """返回指定 message 的 SDK，用于测试 chat_turn 归一化。"""

    def __init__(self, message) -> None:
        self.message = message
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=self.message)], usage=None)


def _tool_call(call_id="call_1", name="get_weather", arguments='{"city": "重庆"}'):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


# -- chat_json ------------------------------------------------------------


def test_chat_json_success_on_first_attempt():
    sdk = ScriptedSDK(['{"city": "重庆", "temp_c": 28.5}'])
    result = LLMClient(sdk).chat_json([{"role": "user", "content": "重庆天气"}], Weather)

    assert isinstance(result, Weather)
    assert result.city == "重庆"
    assert len(sdk.calls) == 1
    assert sdk.calls[0]["response_format"] == {"type": "json_object"}
    assert sdk.calls[0]["model"] == "deepseek-chat"
    assert sdk.calls[0]["timeout"] == 30.0


def test_chat_json_retries_and_feeds_error_back():
    sdk = ScriptedSDK(["这不是 JSON", '{"city": "重庆", "temp_c": 28.5}'])
    result = LLMClient(sdk).chat_json([{"role": "user", "content": "重庆天气"}], Weather)

    assert result.temp_c == 28.5
    assert len(sdk.calls) == 2
    retry_messages = sdk.calls[1]["messages"]
    assert retry_messages[-2] == {"role": "assistant", "content": "这不是 JSON"}
    assert "JSON" in retry_messages[-1]["content"]


def test_chat_json_retries_on_schema_violation():
    sdk = ScriptedSDK(['{"city": "重庆"}', '{"city": "重庆", "temp_c": 20}'])
    result = LLMClient(sdk).chat_json([{"role": "user", "content": "x"}], Weather)

    assert result.temp_c == 20
    assert len(sdk.calls) == 2


def test_chat_json_exhausts_attempts_then_raises():
    sdk = ScriptedSDK(["bad", "still bad"])
    client = LLMClient(sdk, max_json_calls=2)

    with pytest.raises(JSONValidationError) as excinfo:
        client.chat_json([{"role": "user", "content": "x"}], Weather)

    assert excinfo.value.attempts == 2
    assert excinfo.value.schema_name == "Weather"
    assert excinfo.value.last_raw == "still bad"
    assert len(sdk.calls) == 2  # 不会超过 max_json_calls


def test_chat_json_respects_single_attempt_config():
    sdk = ScriptedSDK(["bad", '{"city": "A", "temp_c": 1}'])
    with pytest.raises(JSONValidationError):
        LLMClient(sdk, max_json_calls=1).chat_json([{"role": "user", "content": "x"}], Weather)
    assert len(sdk.calls) == 1


def test_chat_json_does_not_mutate_caller_messages():
    messages = [{"role": "user", "content": "x"}]
    sdk = ScriptedSDK(["bad", '{"city": "A", "temp_c": 1}'])
    LLMClient(sdk).chat_json(messages, Weather)
    assert messages == [{"role": "user", "content": "x"}]


def test_chat_json_records_retry_in_tracer():
    class Recorder:
        def __init__(self):
            self.events = []

        def log(self, kind, **payload):
            self.events.append((kind, payload))

    recorder = Recorder()
    sdk = ScriptedSDK(["bad", '{"city": "A", "temp_c": 1}'])
    LLMClient(sdk, tracer=recorder).chat_json([{"role": "user", "content": "x"}], Weather)

    kinds = [kind for kind, _ in recorder.events]
    assert "chat_json_invalid" in kinds
    assert kinds.count("llm_call") == 2


def test_max_json_calls_must_be_positive():
    with pytest.raises(ValueError):
        LLMClient(ScriptedSDK([]), max_json_calls=0)


# -- 原生 Function Calling 归一化 ------------------------------------------


def test_chat_turn_normalizes_thought_and_tool_calls():
    message = SimpleNamespace(content="先查一下天气", tool_calls=[_tool_call()])
    turn = LLMClient(TurnSDK(message)).chat_turn(
        [{"role": "user", "content": "x"}], tools=[{"type": "function"}]
    )

    assert turn.thought == "先查一下天气"
    assert len(turn.tool_calls) == 1
    call = turn.tool_calls[0]
    assert (call.call_id, call.tool_name) == ("call_1", "get_weather")
    assert call.arguments == {"city": "重庆"}


def test_chat_turn_allows_missing_thought():
    message = SimpleNamespace(content=None, tool_calls=[_tool_call(arguments="")])
    turn = LLMClient(TurnSDK(message)).chat_turn([{"role": "user", "content": "x"}])

    assert turn.thought is None
    assert turn.tool_calls[0].arguments == {}


def test_chat_turn_tolerates_malformed_arguments():
    message = SimpleNamespace(content=None, tool_calls=[_tool_call(arguments="not json")])
    turn = LLMClient(TurnSDK(message)).chat_turn([{"role": "user", "content": "x"}])

    assert turn.tool_calls[0].arguments == {}


def test_chat_turn_without_tool_calls_returns_final():
    message = SimpleNamespace(content="直接回答", tool_calls=None)
    turn = LLMClient(TurnSDK(message)).chat_turn([{"role": "user", "content": "x"}])

    assert turn.tool_calls == []
    assert turn.thought == "直接回答"


# -- 错误封装 --------------------------------------------------------------


def test_sdk_exception_wrapped_as_llm_error():
    class BoomSDK:
        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

        def _create(self, **kwargs):
            raise TimeoutError("timeout")

    with pytest.raises(LLMError, match="timeout"):
        LLMClient(BoomSDK()).chat_text([{"role": "user", "content": "x"}])
