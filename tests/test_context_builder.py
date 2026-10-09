"""ContextBuilder 测试：动态注入与 token 预算（design.md 10 / 12.5）。"""

from __future__ import annotations

import pytest

from first_agent.core.context import (
    DEFAULT_SYSTEM_PROMPT,
    ContextBuilder,
    ContextInputs,
    estimate_tokens,
)
from first_agent.schemas.memory import UserProfile
from first_agent.schemas.session import SessionSummary, WorkingState


def test_system_prompt_includes_profile_state_and_summary():
    prompt = ContextBuilder().render_system_prompt(
        profile=UserProfile.from_memories({"home_city": "重庆", "user_name": "张三"}),
        working_state=WorkingState(
            user_id="u", session_id="s", destination="北京", pending_slots=["出发地"]
        ),
        summary=SessionSummary(
            session_id="s", covered_through_turn=2, summary="用户想看电影", token_count=9
        ),
    )

    assert "home_city：重庆" in prompt
    assert "user_name：张三" in prompt
    assert "destination：北京" in prompt
    assert "仍在等待用户补充：出发地" in prompt
    assert "用户想看电影" in prompt
    assert "以本轮输入为准" in prompt


def test_system_prompt_without_extras_is_base_prompt():
    prompt = ContextBuilder().render_system_prompt()

    assert prompt == DEFAULT_SYSTEM_PROMPT
    assert "Markdown" in prompt  # 终端场景不应出现 Markdown 标记


def test_build_messages_injects_history_and_current_input():
    messages = ContextBuilder().build_messages(
        ContextInputs(
            user_input="那明天呢",
            recent_turns=[
                {"role": "user", "content": "北京天气"},
                {"role": "assistant", "content": "晴"},
            ],
            profile=UserProfile.from_memories({"home_city": "重庆"}),
        )
    )

    assert [message["role"] for message in messages] == ["system", "user", "assistant", "user"]
    assert messages[1]["content"] == "北京天气"
    assert messages[-1]["content"] == "那明天呢"
    assert "重庆" in messages[0]["content"]


def test_recent_turns_window_is_capped():
    builder = ContextBuilder(recent_turns_keep=2)
    turns = [{"role": "user", "content": f"u{index}"} for index in range(10)]

    messages = builder.build_messages(ContextInputs(user_input="now", recent_turns=turns))

    assert [message["content"] for message in messages[1:-1]] == ["u8", "u9"]


def test_budget_trims_history_proportionally():
    turns = [{"role": "user", "content": "很长的历史内容" * 20} for _ in range(8)]
    generous = ContextBuilder(
        max_context_tokens=20000, output_reserve_tokens=1000, recent_turns_keep=50
    )
    tight = ContextBuilder(
        max_context_tokens=1200, output_reserve_tokens=200, recent_turns_keep=50
    )

    assert len(generous.build_messages(ContextInputs(user_input="问", recent_turns=turns))) == 10
    assert len(tight.build_messages(ContextInputs(user_input="问", recent_turns=turns))) < 10


def test_budget_never_drops_system_or_current_input():
    builder = ContextBuilder(
        max_context_tokens=120, output_reserve_tokens=20, recent_turns_keep=10
    )
    turns = [{"role": "user", "content": "x" * 5000} for _ in range(3)]

    messages = builder.build_messages(ContextInputs(user_input="问题", recent_turns=turns))

    assert [message["role"] for message in messages] == ["system", "user"]
    assert messages[-1]["content"] == "问题"


def test_invalid_budget_configuration_rejected():
    with pytest.raises(ValueError):
        ContextBuilder(max_context_tokens=100, output_reserve_tokens=100)


def test_estimate_tokens_weighs_cjk_higher_than_latin():
    assert estimate_tokens("") == 0
    assert estimate_tokens("你好世界", safety_factor=1.0) == 5
    assert estimate_tokens("abcdefgh", safety_factor=1.0) == 3
    assert estimate_tokens("你好", safety_factor=1.2) > estimate_tokens("你好", safety_factor=1.0)
