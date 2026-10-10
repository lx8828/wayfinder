"""门面层的记忆钩子测试：后置写入、失败旁路（design.md 4 节 / 12.5）。"""

from __future__ import annotations

from datetime import datetime, timezone

from wayfinder.core.client import LLMClient
from wayfinder.core.db import Database
from wayfinder.core.engine import ToolAgentRuntime
from wayfinder.core.session import SessionResolver, SessionStore
from wayfinder.evals.scripted import ScriptedLLM, json_obj, text
from wayfinder.memory.service import MemoryAction, MemoryService, RememberOutcome
from wayfinder.memory.store import MemoryStore
from wayfinder.providers import FakeWeatherProvider
from wayfinder.runtime import ReactEngine
from wayfinder.tools.builtin import make_time_tool, make_weather_tool
from wayfinder.tools.registry import ToolRegistry

FIXED_CLOCK = lambda: datetime(2026, 10, 10, 8, 30, tzinfo=timezone.utc)  # noqa: E731


class StubMemoryService:
    def __init__(self, actions=None, *, raises: bool = False) -> None:
        self._actions = actions or {}
        self.raises = raises
        self.calls: list[tuple[str, str]] = []

    def remember(self, user_input, *, user_id) -> RememberOutcome:
        self.calls.append((user_input, user_id))
        if self.raises:
            raise RuntimeError("memory backend down")
        return RememberOutcome(
            actions=[MemoryAction(key=key, action=value) for key, value in self._actions.items()]
        )


def make_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(make_weather_tool(FakeWeatherProvider(), today=lambda: "2026-10-10"))
    registry.register(make_time_tool(clock=FIXED_CLOCK))
    return registry


def build_runtime(script, *, memory_service=None, remember_after_turn=True, tracer=None):
    db = Database(":memory:")
    sdk = ScriptedLLM(script)
    runtime = ToolAgentRuntime(
        engine=ReactEngine(LLMClient(sdk), make_registry(), tracer=tracer),
        sessions=SessionStore(db),
        memories=MemoryStore(db),
        memory_service=memory_service,
        remember_after_turn=remember_after_turn,
        resolver=SessionResolver(id_factory=lambda: "s_fixed"),
        tracer=tracer,
    )
    return runtime, sdk


def test_actions_are_recorded_on_the_outcome():
    service = StubMemoryService({"home_city": "add"})
    runtime, _ = build_runtime([text("好的")], memory_service=service)

    outcome = runtime.chat("我住在重庆", user_id="u1", session_id="s1")

    assert outcome.memory_actions == {"home_city": "add"}
    assert service.calls == [("我住在重庆", "u1")]
    assert outcome.result.answer == "好的"


def test_memory_failure_never_breaks_the_answer():
    class Recorder:
        def __init__(self) -> None:
            self.events: list[str] = []

        def log(self, kind, **payload):
            self.events.append(kind)

    recorder = Recorder()
    service = StubMemoryService({"home_city": "add"}, raises=True)
    runtime, _ = build_runtime([text("好的")], memory_service=service, tracer=recorder)

    outcome = runtime.chat("我住在重庆", user_id="u1", session_id="s1")

    assert outcome.result.answer == "好的"  # 记忆是旁路，不影响回答
    assert outcome.memory_actions == {}
    assert "memory_service_error" in recorder.events


def test_remember_after_turn_can_be_disabled():
    service = StubMemoryService({"home_city": "add"})
    runtime, _ = build_runtime([text("好的")], memory_service=service, remember_after_turn=False)

    outcome = runtime.chat("我住在重庆", user_id="u1", session_id="s1")

    assert outcome.memory_actions == {}
    assert service.calls == []


def test_without_memory_service_is_a_noop():
    runtime, _ = build_runtime([text("好的")])

    outcome = runtime.chat("我住在重庆", user_id="u1", session_id="s1")

    assert outcome.memory_actions == {}
    assert outcome.memory_extraction_failed is False


def test_real_service_writes_memory_and_next_turn_sees_it():
    """后置写入 + 下一轮动态注入的联动。

    真实引擎与记忆服务**共用同一个 ScriptedLLM**，因此脚本消耗顺序为：
    第 1 轮 ReAct → 第 1 轮记忆抽取 → 第 2 轮 ReAct。
    """

    sdk = ScriptedLLM(
        [
            text("好的，记住了"),
            json_obj(
                {
                    "memories": [
                        {"key": "home_city", "value": "重庆", "category": "user_identity"},
                        {"key": "user_name", "value": "张三", "category": "user_identity"},
                    ]
                }
            ),
            text("你好，张三"),
        ]
    )

    db = Database(":memory:")
    memories = MemoryStore(db)
    runtime = ToolAgentRuntime(
        engine=ReactEngine(LLMClient(sdk), make_registry()),
        sessions=SessionStore(db),
        memories=memories,
        memory_service=MemoryService(LLMClient(sdk), memories),
        resolver=SessionResolver(id_factory=lambda: "s_fixed"),
    )

    first = runtime.chat("我叫张三，住在重庆", user_id="u1", session_id="s1")
    assert first.memory_actions == {"home_city": "add", "user_name": "add"}
    assert memories.values("u1") == {"home_city": "重庆", "user_name": "张三"}

    runtime.chat("你好", user_id="u1", session_id="s1")
    second_prompt = sdk.calls[2]["messages"][0]["content"]

    assert "home_city：重庆" in second_prompt
    assert "user_name：张三" in second_prompt
