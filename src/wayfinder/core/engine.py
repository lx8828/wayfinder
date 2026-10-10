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
from typing import Any, Callable, Mapping, Protocol, Sequence

from wayfinder.core.budget import DEFAULT_BUDGET_MS, RunBudget
from wayfinder.core.context import (
    RECENT_TURNS_KEEP,
    SUMMARY_TRIGGER_TURNS,
    ContextBuilder,
    ContextInputs,
)
from wayfinder.core.session import SessionRef, SessionResolver, SessionStore
from wayfinder.memory.store import MemoryStore
from wayfinder.runtime.react import ReactResult


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
    # 本轮实际耗时（用于观察是否逼近预算上限）
    budget_spent_ms: int = 0
    # 本轮结束后的后置摘要动作：nothing_to_do / below_threshold / summarized / failed
    summary_action: str | None = None
    summary_covered_through: int | None = None


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
        budget_ms: int = DEFAULT_BUDGET_MS,
        clock: Callable[[], float] | None = None,
        tracer: Any = None,
        recent_turns_keep: int = RECENT_TURNS_KEEP,
        summarizer: Any = None,
        summary_trigger_turns: int = SUMMARY_TRIGGER_TURNS,
    ) -> None:
        self.engine = engine
        self.plan_engine = plan_engine
        self.default_mode = default_mode
        self.budget_ms = budget_ms
        self.clock = clock
        self.sessions = sessions
        self.memories = memories
        self.resolver = resolver or SessionResolver()
        self.context_builder = context_builder or ContextBuilder()
        self.memory_service = memory_service
        self.remember_after_turn = remember_after_turn
        self.tracer = tracer
        self.recent_turns_keep = recent_turns_keep
        self.summarizer = summarizer
        self.summary_trigger_turns = summary_trigger_turns

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
        # 预算覆盖整轮（引擎 + 后置记忆），由门面统一持有（design.md 14.1）
        budget = RunBudget(budget_ms=self.budget_ms, clock=self.clock)
        ref = self.resolver.resolve(user_id=user_id, session_id=session_id)
        self.sessions.ensure_session(ref)

        working_state = self.sessions.load_working_state(ref)
        profile = self.memories.profile(ref.user_id)
        summary = self.sessions.load_summary(ref)
        window = self._recent_window(ref, summary)
        history = self.sessions.recent_turns(ref, window)

        messages = self.context_builder.build_messages(
            ContextInputs(
                user_input=user_input,
                profile=profile,
                working_state=working_state,
                summary=summary,
                recent_turns=history,
                recent_turns_limit=window,
            )
        )

        result = engine.run_messages(
            messages,
            working_state=working_state,
            defaults=self._engine_defaults(profile, user_id=ref.user_id),
            budget=budget,
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
            result=result,
            ref=ref,
            turn_index=turn_index,
            mode=selected_mode,
            budget_spent_ms=budget.spent_ms,
        )

        if self.memory_service is not None and self.remember_after_turn:
            if budget.exhausted:
                # 预算已耗尽：不再发起额外 LLM 调用（design.md 14.1「停止发起新调用」）
                self._trace("memory_skipped_budget", spent_ms=budget.spent_ms)
            else:
                (
                    outcome.memory_actions,
                    outcome.memory_dropped,
                    outcome.memory_extraction_failed,
                ) = self._remember(user_input, ref.user_id)
            outcome.budget_spent_ms = budget.spent_ms

        if self.summarizer is not None:
            self._summarize(ref, outcome, budget)

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

    def _recent_window(self, ref: SessionRef, summary: Any) -> int:
        """本轮保留多少轮**原文**（design.md 10.1）。

        - 摘要已覆盖到最近窗口之前 → 只保留 ``recent_turns_keep`` 轮（保底窗口）
        - 摘要缺失或未覆盖（含摘要失败）→ 最多保留 ``summary_trigger_turns`` 轮

        第二种情况是**宁多勿丢**：摘要没跟上时若只留 8 轮，中间那些轮次既不在摘要里、
        也不在窗口里，等于静默丢失历史。
        """

        total = self.sessions.turn_count(ref)
        keep = self.recent_turns_keep
        covered = summary.covered_through_turn if summary is not None else -1
        if covered >= total - keep - 1:
            return keep
        return max(keep, min(total, self.summary_trigger_turns))

    def _summarize(self, ref: SessionRef, outcome: ChatOutcome, budget: RunBudget) -> None:
        """后置摘要。**失败绝不影响本轮回答**（design.md 10.3）。"""

        if budget.exhausted:
            self._trace("session_summary_skipped_budget", spent_ms=budget.spent_ms)
            return
        try:
            result = self.summarizer.maybe_summarize(ref)
        except Exception as exc:  # noqa: BLE001 - 摘要是旁路，绝不冒泡
            self._trace("session_summary_error", error=repr(exc))
            return
        outcome.summary_action = result.action
        outcome.summary_covered_through = result.covered_through_turn

    @staticmethod
    def _engine_defaults(profile: Any, *, user_id: str | None = None) -> dict[str, Any]:
        """会话级环境参数：注入用户时区与 ``user_id``（工具里显式传参仍然优先）。

        ``user_id`` 是**模型不可能知道**的参数（如 ``get_calendar_events`` 需要它），
        必须由运行时注入。此前只在 Plan 模式注入，导致该工具在 ReAct 模式下
        **永远无法成功调用**——这是 S7 评测用例暴露出来的缺陷。
        """

        defaults: dict[str, Any] = {}
        timezone = getattr(profile, "timezone", None)
        if timezone:
            defaults["timezone"] = timezone
        if user_id:
            defaults["user_id"] = user_id
        return defaults

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
