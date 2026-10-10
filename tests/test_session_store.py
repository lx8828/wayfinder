"""SessionStore / SessionResolver 测试（design.md 8.3 / 9 / 10）。"""

from __future__ import annotations

from wayfinder.core.db import Database
from wayfinder.core.session import SessionRef, SessionResolver, SessionStore
from wayfinder.schemas.session import SessionSummary

REF = SessionRef(user_id="u1", session_id="s1")


def make_store() -> SessionStore:
    return SessionStore(Database(":memory:"))


def test_working_state_defaults_then_round_trips():
    store = make_store()
    store.ensure_session(REF)

    state = store.load_working_state(REF)
    assert state.user_id == "u1"
    assert state.destination is None

    state.destination = "重庆"
    state.pending_slots = ["出发地"]
    store.save_working_state(state)

    reloaded = store.load_working_state(REF)
    assert reloaded.destination == "重庆"
    assert reloaded.pending_slots == ["出发地"]  # 多轮追问状态跨轮保留（v1 的缺陷）


def test_working_state_is_isolated_per_session():
    store = make_store()
    store.ensure_session(REF)
    other = SessionRef(user_id="u1", session_id="s2")
    store.ensure_session(other)

    state = store.load_working_state(REF)
    state.destination = "北京"
    store.save_working_state(state)

    assert store.load_working_state(other).destination is None


def test_append_turn_increments_index_and_returns_history():
    store = make_store()
    store.ensure_session(REF)

    first = store.append_turn(
        REF,
        user_input="你好",
        assistant_output="你好呀",
        messages=[
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好呀"},
        ],
    )
    second = store.append_turn(REF, user_input="天气", assistant_output="晴")

    assert (first, second) == (0, 1)
    assert store.turn_count(REF) == 2

    history = store.recent_turns(REF, limit=8)
    assert [message["role"] for message in history] == ["user", "assistant", "user", "assistant"]
    assert history[0]["content"] == "你好"  # system 消息被剔除
    assert history[-1]["content"] == "晴"


def test_recent_turns_respects_limit_and_order():
    store = make_store()
    store.ensure_session(REF)
    for index in range(5):
        store.append_turn(REF, user_input=f"u{index}", assistant_output=f"a{index}")

    history = store.recent_turns(REF, limit=2)

    assert [message["content"] for message in history] == ["u3", "a3", "u4", "a4"]


def test_recent_turns_with_zero_limit():
    store = make_store()
    store.ensure_session(REF)
    store.append_turn(REF, user_input="u", assistant_output="a")

    assert store.recent_turns(REF, limit=0) == []


def test_recent_turns_falls_back_when_messages_json_is_corrupt():
    store = make_store()
    store.ensure_session(REF)
    store.db.execute(
        """
        INSERT INTO turns
            (session_id, turn_index, user_input, assistant_output, messages_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("s1", 0, "u", "a", "{不是合法 JSON", "2026-01-01T00:00:00"),
    )

    history = store.recent_turns(REF)

    assert [message["content"] for message in history] == ["u", "a"]


def test_summary_round_trip():
    store = make_store()
    store.ensure_session(REF)
    assert store.load_summary(REF) is None

    store.save_summary(
        SessionSummary(
            session_id="s1", covered_through_turn=3, summary="聊了天气", token_count=12
        )
    )
    loaded = store.load_summary(REF)

    assert loaded is not None
    assert loaded.covered_through_turn == 3
    assert loaded.summary == "聊了天气"
    assert loaded.updated_at  # 自动补时间戳


def test_ensure_session_is_idempotent():
    store = make_store()
    store.ensure_session(REF)
    store.ensure_session(REF)

    rows = store.db.query("SELECT COUNT(*) AS total FROM sessions")
    assert rows[0]["total"] == 1


def test_resolver_generates_session_id_when_missing():
    resolver = SessionResolver(id_factory=lambda: "s_fixed")

    assert resolver.resolve(user_id="u1").session_id == "s_fixed"
    assert resolver.resolve(user_id="u1", session_id="s9").session_id == "s9"


def test_resolver_falls_back_to_default_user():
    assert SessionResolver().resolve().user_id == "anonymous"
    assert SessionResolver().resolve(user_id="  ").user_id == "anonymous"
