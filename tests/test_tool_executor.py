"""工具执行器测试：校验、超时、重试与结构化错误（design.md 11 / 14）。"""

from __future__ import annotations

import time

from pydantic import BaseModel, Field

from wayfinder.providers.base import ProviderNotFound, ProviderTimeout, ProviderUnavailable
from wayfinder.schemas.tool import ToolCall, ToolSpec
from wayfinder.tools.executor import ToolExecutor
from wayfinder.tools.registry import ToolRegistry


class EchoIn(BaseModel):
    text: str = Field(description="要回显的文本")
    times: int = Field(default=1, ge=1)


class EchoOut(BaseModel):
    echoed: str


def echo(payload: EchoIn) -> dict:
    return {"echoed": payload.text * payload.times}


def make_executor(handler=echo, *, max_workers: int = 4, **spec_kwargs) -> ToolExecutor:
    spec_kwargs.setdefault("description", "回显")
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="echo", input_model=EchoIn, output_model=EchoOut, handler=handler, **spec_kwargs)
    )
    return ToolExecutor(registry, max_workers=max_workers)


def call(arguments: dict) -> ToolCall:
    return ToolCall(call_id="c1", tool_name="echo", arguments=arguments)


def test_successful_call_returns_validated_dict():
    result = make_executor().execute(call({"text": "ab", "times": 2}))

    assert result.ok is True
    assert result.data == {"echoed": "abab"}
    assert result.latency_ms >= 0


def test_unknown_tool_returns_structured_error():
    result = make_executor().execute(ToolCall(call_id="c", tool_name="nope"))

    assert result.ok is False
    assert result.error_code == "unknown_tool"


def test_invalid_arguments_are_not_executed():
    executed: list[EchoIn] = []

    def handler(payload: EchoIn) -> dict:
        executed.append(payload)
        return {"echoed": payload.text}

    result = make_executor(handler=handler).execute(call({}))  # text 必填

    assert result.error_code == "invalid_arguments"
    assert "text" in result.error_message
    assert executed == []


def test_optional_field_uses_default():
    assert make_executor().execute(call({"text": "a"})).data == {"echoed": "a"}


def test_provider_error_maps_to_error_code():
    def handler(payload: EchoIn) -> dict:
        raise ProviderTimeout("boom")

    result = make_executor(handler=handler, max_retries=0).execute(call({"text": "a"}))

    assert result.error_code == "timeout"
    assert "boom" in result.error_message


def test_timeout_is_enforced():
    def handler(payload: EchoIn) -> dict:
        time.sleep(0.5)
        return {"echoed": "late"}

    result = make_executor(handler=handler, timeout_s=0.05, max_retries=0).execute(call({"text": "a"}))

    assert result.error_code == "timeout"


def test_transient_failure_is_retried_then_succeeds():
    attempts = {"n": 0}

    def handler(payload: EchoIn) -> dict:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ProviderUnavailable("temporary")
        return {"echoed": "ok"}

    result = make_executor(handler=handler, max_retries=1).execute(call({"text": "a"}))

    assert result.ok is True
    assert attempts["n"] == 2


def test_not_found_is_not_retried():
    attempts = {"n": 0}

    def handler(payload: EchoIn) -> dict:
        attempts["n"] += 1
        raise ProviderNotFound("no such city")

    result = make_executor(handler=handler, max_retries=3).execute(call({"text": "a"}))

    assert result.error_code == "not_found"
    assert attempts["n"] == 1


def test_side_effect_tools_are_not_retried():
    attempts = {"n": 0}

    def handler(payload: EchoIn) -> dict:
        attempts["n"] += 1
        raise ProviderUnavailable("temporary")

    result = make_executor(handler=handler, max_retries=3, side_effect=True).execute(call({"text": "a"}))

    assert result.error_code == "unavailable"
    assert attempts["n"] == 1


def test_invalid_output_is_detected():
    def handler(payload: EchoIn) -> dict:
        return {"wrong": 1}

    result = make_executor(handler=handler).execute(call({"text": "a"}))

    assert result.error_code == "invalid_output"
    assert "EchoOut" in result.error_message


def test_execute_all_preserves_order():
    def handler(payload: EchoIn) -> dict:
        if payload.text == "slow":
            time.sleep(0.05)
        return {"echoed": payload.text}

    executor = make_executor(handler=handler, max_workers=4)
    calls = [
        ToolCall(call_id=f"c{index}", tool_name="echo", arguments={"text": name})
        for index, name in enumerate(["slow", "fast1", "fast2", "fast3"])
    ]

    results = executor.execute_all(calls)
    executor.close()

    assert [result.call_id for result in results] == ["c0", "c1", "c2", "c3"]
    assert all(result.ok for result in results)


def test_execute_all_with_empty_input():
    assert make_executor().execute_all([]) == []


def test_max_workers_must_be_positive():
    registry = ToolRegistry()
    with_error = None
    try:
        ToolExecutor(registry, max_workers=0)
    except ValueError as exc:  # noqa: PERF203 - 明确断言异常
        with_error = exc
    assert with_error is not None


def test_tracer_receives_tool_events():
    class Recorder:
        def __init__(self) -> None:
            self.events: list[str] = []

        def log(self, kind, **payload):
            self.events.append(kind)

    recorder = Recorder()
    registry = ToolRegistry()
    registry.register(
        ToolSpec(name="echo", description="d", input_model=EchoIn, output_model=EchoOut, handler=echo)
    )
    executor = ToolExecutor(registry, tracer=recorder)
    executor.execute(call({"text": "a"}))

    assert "tool_call" in recorder.events
    assert "tool_result" in recorder.events
