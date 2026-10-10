"""短期记忆摘要：触发、幂等、失败降级、窗口衔接（design.md 10.1 / 10.3）。

这里钉住的核心不变量是**不丢轮次**：
``摘要覆盖范围 + 最近原文窗口 = 全部轮次``。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from wayfinder.core.client import LLMClient
from wayfinder.core.db import Database
from wayfinder.core.engine import ToolAgentRuntime
from wayfinder.core.session import SessionRef, SessionResolver, SessionStore
from wayfinder.evals.scripted import ScriptedLLM, json_obj, json_raw, text
from wayfinder.memory.store import MemoryStore
from wayfinder.memory.summarizer import SessionSummarizer
from wayfinder.providers import FakeWeatherProvider
from wayfinder.runtime import ReactEngine
from wayfinder.schemas.session import SessionSummary
from wayfinder.tools.builtin import make_time_tool, make_weather_tool
from wayfinder.tools.registry import ToolRegistry

FIXED_CLOCK = lambda: datetime(2026, 10, 10, 8, 30, tzinfo=timezone.utc)  # noqa: E731


class Recorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def log(self, kind, **payload):
        self.events.append((kind, payload))

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.events]

    def payload(self, kind: str) -> dict:
        return next(payload for name, payload in self.events if name == kind)


def make_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(make_weather_tool(FakeWeatherProvider(), today=lambda: "2026-10-10"))
    registry.register(make_time_tool(clock=FIXED_CLOCK))
    return registry


def build_summarizer(
    script,
    *,
    trigger_turns: int = 16,
    trigger_tokens: int = 16000,
    keep_turns: int = 8,
    tracer=None,
):
    db = Database(":memory:")
    sessions = SessionStore(db)
    sdk = ScriptedLLM(script)
    ref = SessionRef(user_id="u1", session_id="s1")
    sessions.ensure_session(ref)
    summarizer = SessionSummarizer(
        LLMClient(sdk),
        sessions,
        trigger_turns=trigger_turns,
        trigger_tokens=trigger_tokens,
        keep_turns=keep_turns,
        tracer=tracer,
    )
    return summarizer, sessions, sdk, ref


def seed_turns(sessions: SessionStore, ref: SessionRef, count: int, *, prefix: str = "问题") -> None:
    for index in range(count):
        sessions.append_turn(ref, user_input=f"{prefix}{index}", assistant_output=f"回答{index}")


def build_stack(
    script,
    *,
    trigger_turns: int = 16,
    keep_turns: int = 8,
    tracer=None,
    budget_ms: int = 60_000,
    clock=None,
):
    """一个共享同一 ``:memory:`` 库的门面：会话 / 记忆 / 摘要器 / ReAct 引擎。"""

    db = Database(":memory:")
    sessions = SessionStore(db)
    sdk = ScriptedLLM(script)
    client = LLMClient(sdk)
    summarizer = SessionSummarizer(
        client, sessions, trigger_turns=trigger_turns, keep_turns=keep_turns, tracer=tracer
    )
    runtime = ToolAgentRuntime(
        engine=ReactEngine(client, make_registry(), tracer=tracer),
        sessions=sessions,
        memories=MemoryStore(db),
        memory_service=None,
        remember_after_turn=False,
        summarizer=summarizer,
        summary_trigger_turns=trigger_turns,
        resolver=SessionResolver(id_factory=lambda: "s_fixed"),
        tracer=tracer,
        budget_ms=budget_ms,
        clock=clock,
    )
    return runtime, sessions, sdk


def user_messages_in(call: dict) -> list[dict]:
    return [message for message in call["messages"] if message["role"] == "user"]


# -- 摘要器：触发与幂等 ----------------------------------------------------


def test_below_trigger_keeps_all_turns_without_any_llm_call():
    summarizer, sessions, sdk, ref = build_summarizer([])
    seed_turns(sessions, ref, 10)

    outcome = summarizer.maybe_summarize(ref)

    assert outcome.action == "below_threshold"
    assert outcome.summary is None
    assert sdk.calls == []  # 未到阈值不该产生任何调用
    assert sessions.load_summary(ref) is None


def test_summary_generated_when_unsummarized_turns_exceed_trigger():
    summarizer, sessions, sdk, ref = build_summarizer([json_obj({"summary": "用户在排查出行安排"})])
    seed_turns(sessions, ref, 25)

    outcome = summarizer.maybe_summarize(ref)

    assert outcome.action == "summarized"
    assert len(sdk.calls) == 1
    saved = sessions.load_summary(ref)
    assert saved is not None
    assert saved.summary == "用户在排查出行安排"
    assert saved.covered_through_turn == 16  # 25 - keep(8) - 1
    assert saved.token_count > 0


def test_summary_and_recent_window_cover_every_turn():
    """核心不变量：摘要覆盖 + 最近窗口 = 全部轮次，不允许出现缺口。"""

    keep_turns = 8
    total = 25
    summarizer, sessions, _, ref = build_summarizer(
        [json_obj({"summary": "摘要"})], keep_turns=keep_turns
    )
    seed_turns(sessions, ref, total)

    outcome = summarizer.maybe_summarize(ref)

    assert outcome.covered_through_turn + 1 + keep_turns == total


def test_second_call_without_new_turns_is_idempotent():
    summarizer, sessions, sdk, ref = build_summarizer([json_obj({"summary": "摘要"})])
    seed_turns(sessions, ref, 25)
    summarizer.maybe_summarize(ref)
    calls = len(sdk.calls)

    again = summarizer.maybe_summarize(ref)

    assert again.action == "nothing_to_do"
    assert len(sdk.calls) == calls  # 幂等：没有新轮次就不再调 LLM


def test_incremental_summary_merges_previous_and_only_covers_new_turns():
    summarizer, sessions, sdk, ref = build_summarizer(
        [json_obj({"summary": "第一次摘要"}), json_obj({"summary": "合并后的摘要"})]
    )
    seed_turns(sessions, ref, 25)
    summarizer.maybe_summarize(ref)

    seed_turns(sessions, ref, 9)  # 总计 34 轮
    outcome = summarizer.maybe_summarize(ref)

    assert outcome.covered_through_turn == 25  # 34 - 8 - 1
    payload = json.loads(sdk.calls[1]["messages"][-1]["content"])
    assert payload["已有摘要"] == "第一次摘要"
    # 只摘要 covered_through_turn 之后的轮次：17..25
    assert [turn["turn_index"] for turn in payload["新对话"]] == list(range(17, 26))


def test_token_trigger_fires_even_with_few_turns():
    summarizer, sessions, sdk, ref = build_summarizer(
        [json_obj({"summary": "长对话摘要"})], trigger_tokens=100, keep_turns=1
    )
    seed_turns(sessions, ref, 3, prefix="很长的内容" * 50)

    outcome = summarizer.maybe_summarize(ref)

    assert outcome.action == "summarized"
    assert len(sdk.calls) == 1


def test_summary_failure_keeps_state_and_does_not_raise():
    summarizer, sessions, sdk, ref = build_summarizer(
        [json_raw("不是 JSON"), json_raw("还是不是 JSON")], trigger_turns=2, keep_turns=1
    )
    seed_turns(sessions, ref, 5)

    outcome = summarizer.maybe_summarize(ref)

    assert outcome.failed is True
    assert outcome.action == "failed"
    assert sessions.load_summary(ref) is None  # 不留半成品
    assert len(sdk.calls) == 2  # chat_json 内部重试一次后放弃


def test_failed_summary_keeps_the_previous_one():
    summarizer, sessions, sdk, ref = build_summarizer(
        [json_obj({"summary": "旧摘要"}), json_raw("坏的"), json_raw("还是坏的")],
        trigger_turns=2,
        keep_turns=1,
    )
    seed_turns(sessions, ref, 5)
    summarizer.maybe_summarize(ref)

    seed_turns(sessions, ref, 3)
    outcome = summarizer.maybe_summarize(ref)

    assert outcome.failed is True
    assert outcome.covered_through_turn == 3  # 仍是旧摘要的覆盖范围
    assert sessions.load_summary(ref).summary == "旧摘要"


def test_summarized_event_is_traced_with_counts():
    recorder = Recorder()
    summarizer, sessions, _, ref = build_summarizer(
        [json_obj({"summary": "摘要"})], tracer=recorder
    )
    seed_turns(sessions, ref, 25)

    summarizer.maybe_summarize(ref)

    payload = recorder.payload("session_summarized")
    assert payload["covered_through_turn"] == 16
    assert payload["compressed_turns"] == 25
    assert payload["latency_ms"] >= 0


# -- 门面：窗口衔接与失败旁路 ----------------------------------------------


def test_window_narrows_when_summary_covers_earlier_turns():
    runtime, sessions, sdk = build_stack([text("好的")])
    ref = SessionRef(user_id="u1", session_id="s_fixed")
    sessions.ensure_session(ref)
    seed_turns(sessions, ref, 20)
    sessions.save_summary(
        SessionSummary(
            session_id=ref.session_id,
            covered_through_turn=11,
            summary="此前约定：用户周末想去公园",
            token_count=20,
        )
    )

    runtime.chat("那明天呢", user_id="u1", session_id="s_fixed", mode="react")

    call = sdk.calls[0]
    assert "此前约定：用户周末想去公园" in call["messages"][0]["content"]
    assert "已覆盖前 12 轮" in call["messages"][0]["content"]
    assert len(user_messages_in(call)) == 8 + 1  # 保底窗口 8 轮 + 本轮


def test_window_falls_back_to_trigger_when_summary_is_missing():
    """摘要没跟上时宁可多留原文，也不能既不摘要又丢掉中间轮次。"""

    runtime, sessions, sdk = build_stack([text("好的")], trigger_turns=16)
    ref = SessionRef(user_id="u1", session_id="s_fixed")
    sessions.ensure_session(ref)
    seed_turns(sessions, ref, 20)

    runtime.chat("那明天呢", user_id="u1", session_id="s_fixed", mode="react")

    assert len(user_messages_in(sdk.calls[0])) == 16 + 1


def test_turn_without_resummary_is_fully_preserved_below_trigger():
    runtime, sessions, sdk = build_stack([text("好的")], trigger_turns=16)
    ref = SessionRef(user_id="u1", session_id="s_fixed")
    sessions.ensure_session(ref)
    seed_turns(sessions, ref, 6)

    runtime.chat("那明天呢", user_id="u1", session_id="s_fixed", mode="react")

    assert len(user_messages_in(sdk.calls[0])) == 6 + 1  # 未到窗口上限 → 全部保留


def test_engine_summarizes_after_the_turn():
    runtime, sessions, sdk = build_stack([text("好的"), json_obj({"summary": "长期对话摘要"})])
    ref = SessionRef(user_id="u1", session_id="s_fixed")
    sessions.ensure_session(ref)
    seed_turns(sessions, ref, 25)

    outcome = runtime.chat("那明天呢", user_id="u1", session_id="s_fixed", mode="react")

    saved = sessions.load_summary(ref)
    assert saved is not None and saved.summary == "长期对话摘要"
    assert saved.covered_through_turn == 17  # 26 轮 - keep(8) - 1
    assert outcome.summary_action == "summarized"
    assert outcome.summary_covered_through == 17
    assert outcome.result.answer == "好的"  # 回答不受摘要影响


def test_summary_failure_does_not_block_the_answer():
    runtime, sessions, sdk = build_stack(
        [text("好的"), json_raw("坏的"), json_raw("还是坏的")],
        trigger_turns=2,
        keep_turns=1,
    )
    ref = SessionRef(user_id="u1", session_id="s_fixed")
    sessions.ensure_session(ref)
    seed_turns(sessions, ref, 5)

    outcome = runtime.chat("那明天呢", user_id="u1", session_id="s_fixed", mode="react")

    assert outcome.result.answer == "好的"
    assert outcome.summary_action == "failed"
    assert sessions.load_summary(ref) is None


class SteppingClock:
    """每次读取前进固定步长的假时钟（首次读取作为起点）。"""

    def __init__(self, step_seconds: float) -> None:
        self._step = step_seconds
        self._seconds = 0.0
        self._reads = 0

    def __call__(self) -> float:
        self._reads += 1
        if self._reads > 1:
            self._seconds += self._step
        return self._seconds


def test_summary_is_skipped_when_budget_is_exhausted():
    recorder = Recorder()
    runtime, sessions, sdk = build_stack(
        [text("好的"), json_obj({"summary": "不该被写入"})],
        tracer=recorder,
        budget_ms=1_500,
        clock=SteppingClock(1.0),
    )
    ref = SessionRef(user_id="u1", session_id="s_fixed")
    sessions.ensure_session(ref)
    seed_turns(sessions, ref, 25)

    outcome = runtime.chat("那明天呢", user_id="u1", session_id="s_fixed", mode="react")

    assert "session_summary_skipped_budget" in recorder.kinds()
    assert sessions.load_summary(ref) is None
    assert len(sdk.calls) == 1  # 只有引擎那一次调用，摘要在耗尽后不再发起
    assert outcome.summary_action is None
