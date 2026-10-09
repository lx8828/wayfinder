"""对外 Runtime 门面（组合根）。对应 design.md 4 / 9 / 10 / 12.5。

职责：把 ``ContextBuilder`` + ``SessionStore`` + ``MemoryStore`` + 具体引擎接起来：

    解析会话 → 读 WorkingState → 读长期记忆 → 读摘要与最近轮次
    → 组装上下文（动态注入）→ 交给引擎跑 → 持久化状态与本轮消息

> 依赖说明：本模块是**组合根**，也是唯一允许跨层 import 的地方
> （``core → memory`` / ``runtime`` / ``tools``）。design 第 6 节的依赖图未画出
> 这条边，应补一句「组合根豁免」。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, Sequence

from first_agent.core.context import RECENT_TURNS_KEEP, ContextBuilder, ContextInputs
from first_agent.core.session import SessionRef, SessionResolver, SessionStore
from first_agent.memory.store import MemoryStore
from first_agent.runtime.react import ReactResult


class _Engine(Protocol):
    """``ReactEngine`` 与 ``PlanExecuteEngine`` 的共同入口。"""

    def run_messages(
        self,
        messages: list[dict],
        *,
        working_state: Any = None,
        defaults: Mapping[str, Any] | None = None,
    ) -> ReactResult: ...


@dataclass
class ChatOutcome:
    result: ReactResult
    ref: SessionRef
    turn_index: int
    mode: str = "react"
    # 本轮后置记忆动作：{key: add/keep/update/skip}，供评测的 expected_memory_delta 对比
    memory_actions: dict[str, str] = field(default_factory=dict)
    memory_dropped: list[dict[str, Any]] = field(default_factory=list)
    # 记忆抽取的结构化输出是否失败（用于评测的 Structured Output Validity Rate）
    memory_extraction_failed: bool = False


class ToolAgentRuntime:
    def __init__(
        self,
        *,
        engine: _Engine,
        sessions: SessionStore,
        memories: MemoryStore,
        resolver: SessionResolver | None = None,
        context_builder: ContextBuilder | None = None,
        memory_service: Any = None,
        remember_after_turn: bool = True,
        plan_engine: _Engine | None = None,
        default_mode: str = "react",
        tracer: Any = None,
        recent_turns_keep: int = RECENT_TURNS_KEEP,
    ) -> None:
        self.engine = engine
        self.plan_engine = plan_engine
        self.default_mode = default_mode
        self.sessions = sessions
        self.memories = memories
        self.resolver = resolver or SessionResolver()
        self.context_builder = context_builder or ContextBuilder()
        self.memory_service = memory_service
        self.remember_after_turn = remember_after_turn
        self.tracer = tracer
        self.recent_turns_keep = recent_turns_keep

    # -- 对话 -------------------------------------------------------------
    def chat(
        self,
        user_input: str,
        *,
        user_id: str | None = None,
        session_id: str | None = None,
        mode: str | None = None,
    ) -> ChatOutcome:
        engine, selected_mode = self._select_engine(mode)
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

        result = engine.run_messages(
            messages,
            working_state=working_state,
            defaults=self._engine_defaults(profile),
        )

        # 引擎可能原地更新了 working_state（Plan 会写槽位与 pending_slots）
        self.sessions.save_working_state(working_state)
        turn_index = self.sessions.append_turn(
            ref,
            user_input=user_input,
            assistant_output=result.answer,
            messages=result.messages,
        )

        outcome = ChatOutcome(
            result=result, ref=ref, turn_index=turn_index, mode=selected_mode
        )
        if self.memory_service is not None and self.remember_after_turn:
            (
                outcome.memory_actions,
                outcome.memory_dropped,
                outcome.memory_extraction_failed,
            ) = self._remember(user_input, ref.user_id)
        return outcome

    def _select_engine(self, mode: str | None) -> tuple[_Engine, str]:
        selected = (mode or self.default_mode or "react").strip().lower()
        if selected == "plan":
            if self.plan_engine is None:
                raise ValueError("未配置 Plan-Execute 引擎，无法使用 mode='plan'")
            return self.plan_engine, selected
        if selected != "react":
            raise ValueError(f"未知对话模式：{mode!r}（仅支持 react / plan）")
        return self.engine, selected

    @staticmethod
    def _engine_defaults(profile: Any) -> dict[str, Any]:
        """会话级环境参数：目前注入用户时区（工具里显式传参仍然优先）。"""

        timezone = getattr(profile, "timezone", None)
        return {"timezone": timezone} if timezone else {}

    def _remember(
        self, user_input: str, user_id: str
    ) -> tuple[dict[str, str], list[dict[str, Any]], bool]:
        """后置记忆写入。**任何失败都不能影响本轮回答。**"""

        try:
            remembered = self.memory_service.remember(user_input, user_id=user_id)
        except Exception as exc:  # noqa: BLE001 - 记忆是旁路，绝不冒泡
            self._trace("memory_service_error", error=repr(exc))
            return {}, [], True
        return remembered.actions_by_key, list(remembered.dropped), remembered.extraction_failed
        return remembered.actions_by_key, list(remembered.dropped)

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
