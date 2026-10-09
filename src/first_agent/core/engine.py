"""对外 Runtime 门面（组合根）。对应 design.md 4 / 9 / 10 / 12.5。

职责：把 ``ContextBuilder`` + ``SessionStore`` + ``MemoryStore`` + 具体引擎接起来：

    解析会话 → 读 WorkingState → 读长期记忆 → 读摘要与最近轮次
    → 组装上下文（动态注入）→ 交给引擎跑 → 持久化状态与本轮消息

> 依赖说明：本模块是**组合根**，也是唯一允许跨层 import 的地方
> （``core → memory`` / ``runtime`` / ``tools``）。design 第 6 节的依赖图未画出
> 这条边，应补一句「组合根豁免」。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, Sequence

from first_agent.core.context import RECENT_TURNS_KEEP, ContextBuilder, ContextInputs
from first_agent.core.session import SessionRef, SessionResolver, SessionStore
from first_agent.memory.store import MemoryStore
from first_agent.runtime.react import ReactResult


class _Engine(Protocol):
    def run_messages(self, messages: list[dict]) -> ReactResult: ...


@dataclass
class ChatOutcome:
    result: ReactResult
    ref: SessionRef
    turn_index: int


class ToolAgentRuntime:
    def __init__(
        self,
        *,
        engine: _Engine,
        sessions: SessionStore,
        memories: MemoryStore,
        resolver: SessionResolver | None = None,
        context_builder: ContextBuilder | None = None,
        tracer: Any = None,
        recent_turns_keep: int = RECENT_TURNS_KEEP,
    ) -> None:
        self.engine = engine
        self.sessions = sessions
        self.memories = memories
        self.resolver = resolver or SessionResolver()
        self.context_builder = context_builder or ContextBuilder()
        self.tracer = tracer
        self.recent_turns_keep = recent_turns_keep

    # -- 对话 -------------------------------------------------------------
    def chat(
        self,
        user_input: str,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
    ) -> ChatOutcome:
        ref = self.resolver.resolve(user_id=user_id, session_id=session_id)
        self.sessions.ensure_session(ref)

        working_state = self.sessions.load_working_state(ref)
        profile = self.memories.profile(ref.user_id)
        summary = self.sessions.load_summary(ref)
        history = self.sessions.recent_turns(ref, self.recent_turns_keep)

        messages = self.context_builder.build_messages(
            ContextInputs(
                user_input=user_input,
                profile=profile,
                working_state=working_state,
                summary=summary,
                recent_turns=history,
            )
        )

        result = self.engine.run_messages(messages)

        self.sessions.save_working_state(working_state)
        turn_index = self.sessions.append_turn(
            ref,
            user_input=user_input,
            assistant_output=result.answer,
            messages=result.messages,
        )
        return ChatOutcome(result=result, ref=ref, turn_index=turn_index)

    # -- 记忆写入（确定性部分；LLM 抽取与关系裁决在 S5 接入） --------------
    def remember(self, candidate: Any, relation: str | None = None) -> str:
        action = self.memories.apply_candidate(candidate, relation)
        self._trace(
            "memory_action",
            key=getattr(candidate, "key", None),
            action=action,
            relation=relation,
        )
        return action

    # -- 读 ---------------------------------------------------------------
    def history(self, *, user_id: str, session_id: str) -> Sequence[dict]:
        ref = SessionRef(user_id=user_id, session_id=session_id)
        return self.sessions.recent_turns(ref, self.recent_turns_keep)

    def _trace(self, kind: str, **payload: Any) -> None:
        if self.tracer is not None:
            self.tracer.log(kind, **payload)
