"""ToolAgentRuntime 门面测试：动态记忆注入、跨轮状态与会话隔离（S4 验收）。"""

from __future__ import annotations

from datetime import datetime, timezone

from first_agent.core.client import LLMClient
from first_agent.core.db import Database
from first_agent.core.engine import ToolAgentRuntime
from first_agent.core.session import SessionResolver, SessionStore
from first_agent.evals.scripted import ScriptedLLM, text, tool
from first_agent.memory.store import MemoryStore
from first_agent.providers import FakeWeatherProvider
from first_agent.runtime import ReactEngine
from first_agent.schemas.memory import MemoryCandidate
from first_agent.tools.builtin import make_time_tool, make_weather_tool
from first_agent.tools.registry import ToolRegistry

FIXED_CLOCK = lambda: datetime(2026, 10, 10, 8, 30, tzinfo=timezone.utc)  # noqa: E731


def build_runtime(script, *, tracer=None):
    db = Database(":memory:")
    registry = ToolRegistry()
    registry.register(make_weather_tool(FakeWeatherProvider(), today=lambda: "2026-10-10"))
    registry.register(make_time_tool(clock=FIXED_CLOCK))
    sdk = ScriptedLLM(script)
    runtime = ToolAgentRuntime(
        engine=ReactEngine(LLMClient(sdk), registry, tracer=tracer),
        sessions=SessionStore(db),
        memories=MemoryStore(db),
        resolver=SessionResolver(id_factory=lambda: "s_fixed"),
        tracer=tracer,
    )
    return runtime, sdk


def test_new_memory_is_injected_on_the_next_turn():
    """S4 验收：同会话新记忆下一轮立即生效。"""

    runtime, sdk = build_runtime(
        [tool("get_weather", {"city": "重庆"}, thought="查重庆"), text("重庆今天多云"), text("明天仍多云")]
    )

    runtime.chat("我这里天气怎么样", user_id="u1", session_id="s1")
    assert "home_city" not in sdk.calls[0]["messages"][0]["content"]

    action = runtime.remember(
        MemoryCandidate(user_id="u1", key="home_city", value="重庆", category="user_identity")
    )
    second = runtime.chat("明天呢", user_id="u1", session_id="s1")

    assert action == "add"
    # 第 3 次 LLM 调用 = 第二轮的第一步
    assert "home_city：重庆" in sdk.calls[2]["messages"][0]["content"]
    assert second.result.answer == "明天仍多云"


def test_previous_tool_history_is_replayed_to_the_next_turn():
    runtime, sdk = build_runtime(
        [tool("get_weather", {"city": "北京"}), text("北京晴"), text("不客气")]
    )

    runtime.chat("北京天气", user_id="u1", session_id="s1")
    runtime.chat("谢谢", user_id="u1", session_id="s1")

    roles = [message["role"] for message in sdk.calls[2]["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert sdk.calls[2]["messages"][3]["role"] == "tool"


def test_working_state_pending_slots_survive_across_turns():
    """S4 验收：多轮追问保留任务状态（不再每轮清零）。"""

    runtime, sdk = build_runtime([text("好的"), text("好的")])

    outcome = runtime.chat("我想出门", user_id="u1", session_id="s1")
    state = runtime.sessions.load_working_state(outcome.ref)
    state.purpose = "看电影"
    state.pending_slots = ["目的地"]
    runtime.sessions.save_working_state(state)

    runtime.chat("重庆", user_id="u1", session_id="s1")

    prompt = sdk.calls[1]["messages"][0]["content"]
    assert "purpose：看电影" in prompt
    assert "仍在等待用户补充：目的地" in prompt


def test_sessions_do_not_leak_between_session_ids():
    runtime, sdk = build_runtime([text("第一轮"), text("第二轮")])

    runtime.chat("你好", user_id="u1", session_id="s1")
    runtime.chat("你好", user_id="u1", session_id="s2")

    second_session_messages = sdk.calls[1]["messages"]
    # s2 是全新会话：只有 system + 本轮 user，没有 s1 的历史
    assert [message["role"] for message in second_session_messages] == ["system", "user"]


def test_chat_records_turn_and_generates_session_when_missing():
    runtime, _ = build_runtime([text("hi")])

    outcome = runtime.chat("你好", user_id="u1")

    assert outcome.ref.session_id == "s_fixed"
    assert outcome.turn_index == 0
    assert runtime.sessions.turn_count(outcome.ref) == 1


def test_remember_traces_the_action():
    class Recorder:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        def log(self, kind, **payload):
            self.events.append((kind, payload))

    recorder = Recorder()
    runtime, _ = build_runtime([], tracer=recorder)

    action = runtime.remember(
        MemoryCandidate(user_id="u1", key="home_city", value="重庆", category="user_identity")
    )

    assert action == "add"
    kind, payload = recorder.events[-1]
    assert kind == "memory_action"
    assert payload["action"] == "add"


def test_history_helper_reads_persisted_turns():
    runtime, _ = build_runtime([text("嗨")])
    runtime.chat("你好", user_id="u1", session_id="s1")

    history = runtime.history(user_id="u1", session_id="s1")

    assert [message["role"] for message in history] == ["user", "assistant"]
    assert history[1]["content"] == "嗨"
