"""会话短期记忆摘要（design.md 10.1 / 10.2 / 10.3）。

职责边界：**只做压缩**——把「已摘要范围之后、最近窗口之前」的轮次并进 ``SessionSummary``。

三条硬要求（10.3）：

1. **触发**：未摘要轮次超过 ``trigger_turns``（默认 16）**或**其估计 token 超过
   ``trigger_tokens``（默认 16000）才调用 LLM，否则原样保留原文。
2. **幂等**：幂等键是 ``session_id + covered_through_turn``；没有新轮次时再调用不会产生任何 LLM 请求。
3. **失败不阻塞**：摘要失败保留旧摘要、记录 Trace，当前回答照常返回，
   并且**绝不为摘要而丢轮次**（门面会因此保留更多原文，见 ``core.engine``）。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Sequence

from pydantic import BaseModel

from wayfinder.core.client import JSONValidationError, LLMClient, LLMError
from wayfinder.core.context import (
    RECENT_TURNS_KEEP,
    SUMMARY_TRIGGER_TOKENS,
    SUMMARY_TRIGGER_TURNS,
    estimate_tokens,
)
from wayfinder.core.db import now_iso
from wayfinder.core.session import SessionRef, SessionStore
from wayfinder.schemas.session import SessionSummary

SYSTEM_PROMPT = """你是会话记忆压缩器：把「已有摘要」与「新对话」合并成一段简短摘要。

必须保留：
- 用户的目标、约束与已确认的偏好（地点、时间、交通方式等）
- 已经查到的事实结论（天气、日程、路程耗时等），并注明属于哪一轮
- 尚未解决的问题，以及用户追问过但没得到答案的点

不要：编造未曾出现的信息、保留寒暄、复述工具的原始字段。
只返回 JSON：{"summary": "..."}（不超过 200 字）
"""


class SummaryResult(BaseModel):
    summary: str


@dataclass
class SummaryOutcome:
    """一次摘要尝试的结果（``action`` 是该不该/有没有压缩的唯一真相）。"""

    summary: SessionSummary | None = None
    # nothing_to_do（无新轮次或未到窗口边界）/ below_threshold / summarized / failed
    action: str = "nothing_to_do"
    # 本次调用之后的**有效覆盖范围**（失败时即旧摘要的覆盖范围）
    covered_through_turn: int | None = None
    failed: bool = False

    @property
    def generated(self) -> bool:
        return self.action == "summarized"


class SessionSummarizer:
    def __init__(
        self,
        client: LLMClient,
        sessions: SessionStore,
        *,
        tracer: Any = None,
        trigger_turns: int = SUMMARY_TRIGGER_TURNS,
        trigger_tokens: int = SUMMARY_TRIGGER_TOKENS,
        keep_turns: int = RECENT_TURNS_KEEP,
        max_input_chars: int = 12000,
    ) -> None:
        if trigger_turns < 1 or keep_turns < 0:
            raise ValueError("trigger_turns 至少为 1，keep_turns 不能为负")
        self.client = client
        self.sessions = sessions
        self.tracer = tracer
        self.trigger_turns = trigger_turns
        self.trigger_tokens = trigger_tokens
        self.keep_turns = keep_turns
        self.max_input_chars = max_input_chars

    # -- 入口 -------------------------------------------------------------
    def maybe_summarize(self, ref: SessionRef) -> SummaryOutcome:
        """按需压缩；返回结果而不抛错（摘要永远不能影响本轮回答）。"""

        total = self.sessions.turn_count(ref)
        existing = self.sessions.load_summary(ref)
        covered = existing.covered_through_turn if existing else -1

        # 只压缩「早于最近 keep_turns 轮」的部分：最近窗口始终保留原文（10.1）
        end = total - self.keep_turns - 1
        if end < 0 or end <= covered:
            return SummaryOutcome(existing, "nothing_to_do", covered_through_turn=covered)

        pending = self.sessions.turns_between(ref, covered + 1, end)
        if not pending:
            return SummaryOutcome(existing, "nothing_to_do", covered_through_turn=covered)

        pending_turns = total - covered - 1
        pending_tokens = self._pending_tokens(pending)
        if pending_turns <= self.trigger_turns and pending_tokens <= self.trigger_tokens:
            # 未到触发阈值：保留全部原文（10.1「空闲时保留全部原文」）
            return SummaryOutcome(existing, "below_threshold", covered_through_turn=covered)

        return self._compress(
            ref,
            existing=existing,
            pending=pending,
            end=end,
            pending_turns=pending_turns,
            pending_tokens=pending_tokens,
        )

    # -- 内部 -------------------------------------------------------------
    def _compress(
        self,
        ref: SessionRef,
        *,
        existing: SessionSummary | None,
        pending: Sequence[dict[str, Any]],
        end: int,
        pending_turns: int,
        pending_tokens: int,
    ) -> SummaryOutcome:
        payload = {
            "已有摘要": (existing.summary if existing else "（无）"),
            "新对话": list(pending),
            "要求": '合并成一段摘要，只返回 JSON：{"summary": "..."}',
        }
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(payload, ensure_ascii=False)[: self.max_input_chars],
            },
        ]

        started = time.perf_counter()
        try:
            result = self.client.chat_json(messages, SummaryResult)
        except (JSONValidationError, LLMError) as exc:
            # design 10.3：摘要失败不阻塞回答，保留旧摘要
            self._trace("session_summary_failed", covered_through_turn=end, error=str(exc))
            return SummaryOutcome(
                existing,
                "failed",
                covered_through_turn=(existing.covered_through_turn if existing else None),
                failed=True,
            )

        text = (result.summary or "").strip()
        if not text:
            self._trace("session_summary_empty", covered_through_turn=end)
            return SummaryOutcome(
                existing,
                "failed",
                covered_through_turn=(existing.covered_through_turn if existing else None),
                failed=True,
            )

        summary = SessionSummary(
            session_id=ref.session_id,
            covered_through_turn=end,
            summary=text,
            token_count=estimate_tokens(text),
            updated_at=now_iso(),
        )
        self.sessions.save_summary(summary)
        self._trace(
            "session_summarized",
            covered_through_turn=end,
            compressed_turns=pending_turns,
            source_tokens=pending_tokens,
            summary_tokens=summary.token_count,
            latency_ms=int((time.perf_counter() - started) * 1000),
        )
        return SummaryOutcome(summary, "summarized", covered_through_turn=end)

    @staticmethod
    def _pending_tokens(pending: Sequence[dict[str, Any]]) -> int:
        return estimate_tokens(json.dumps(list(pending), ensure_ascii=False))

    def _trace(self, kind: str, **payload: Any) -> None:
        if self.tracer is not None:
            self.tracer.log(kind, **payload)
