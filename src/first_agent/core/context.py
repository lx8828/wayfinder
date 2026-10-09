"""上下文构建：动态注入长期记忆、任务状态与会话摘要。对应 design.md 12.5 / 10。

设计约束：

- **每轮重新构建**，不复用初始化时的 system prompt（v1 的已知缺陷）
- 本模块是**纯函数式**的（只吃已加载好的数据），不 import memory / runtime，
  以保持 ``core`` 的基础设施定位；装配由 ``core.engine`` 负责
- token 预算用**字符数近似**而非 tiktoken：tiktoken 首次使用需联网下载 BPE
  词表，会破坏「离线可跑」这一硬约束（详见 S4 说明）
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Sequence

from first_agent.schemas.memory import UserProfile
from first_agent.schemas.session import SLOT_FIELDS, SessionSummary, WorkingState

DEFAULT_SYSTEM_PROMPT = (
    "你是一名智能出行助手。\n"
    "回答用户关于出行、天气与时间的问题；需要事实数据时必须调用工具，不要凭记忆编造。\n"
    "工具返回错误时，向用户说明情况并给出可行的替代建议，不要暴露内部堆栈或调试信息。\n"
    "输出纯文本：不要使用 Markdown 语法（如 **加粗**、# 标题、表格、代码块）。\n"
    "先给出结论，再补充关键细节，保持简洁。"
)

MAX_CONTEXT_TOKENS = 24000
OUTPUT_RESERVE_TOKENS = 4000
RECENT_TURNS_KEEP = 8
SAFETY_FACTOR = 1.2

_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def estimate_tokens(text: str, *, safety_factor: float = SAFETY_FACTOR) -> int:
    """近似 token 估算：CJK 字符按 1 token、其他字符按 1/4 token，再乘安全系数。

    用 ``ceil`` 而非截断：安全系数的作用是**宁可高估**，不能因取整而失效。
    """

    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return math.ceil((cjk + other / 4) * safety_factor) + 1


@dataclass
class ContextInputs:
    """一次上下文构建所需的全部数据（由装配层提供）。"""

    user_input: str
    system_prompt: str | None = None
    profile: UserProfile | None = None
    working_state: WorkingState | None = None
    summary: SessionSummary | None = None
    recent_turns: Sequence[dict] = field(default_factory=tuple)


class ContextBuilder:
    def __init__(
        self,
        *,
        max_context_tokens: int = MAX_CONTEXT_TOKENS,
        output_reserve_tokens: int = OUTPUT_RESERVE_TOKENS,
        recent_turns_keep: int = RECENT_TURNS_KEEP,
        safety_factor: float = SAFETY_FACTOR,
    ) -> None:
        if max_context_tokens <= output_reserve_tokens:
            raise ValueError("max_context_tokens 必须大于 output_reserve_tokens")
        self.max_context_tokens = max_context_tokens
        self.output_reserve_tokens = output_reserve_tokens
        self.recent_turns_keep = recent_turns_keep
        self.safety_factor = safety_factor

    @property
    def budget_tokens(self) -> int:
        return self.max_context_tokens - self.output_reserve_tokens

    # -- 组装的对外入口 ---------------------------------------------------
    def build_messages(self, inputs: ContextInputs) -> list[dict]:
        system_message = {
            "role": "system",
            "content": self.render_system_prompt(
                profile=inputs.profile,
                working_state=inputs.working_state,
                summary=inputs.summary,
                base=inputs.system_prompt,
            ),
        }

        history = [dict(message) for message in inputs.recent_turns][-self.recent_turns_keep :]
        user_message = {"role": "user", "content": inputs.user_input}

        # 预算控制：从最旧的历史开始丢弃
        while history and self._tokens(system_message, history, user_message) > self.budget_tokens:
            history.pop(0)

        return [system_message, *history, user_message]

    # -- system prompt ----------------------------------------------------
    def render_system_prompt(
        self,
        *,
        profile: UserProfile | None = None,
        working_state: WorkingState | None = None,
        summary: SessionSummary | None = None,
        base: str | None = None,
    ) -> str:
        sections: list[str] = [base or DEFAULT_SYSTEM_PROMPT]

        if profile is not None and not profile.is_empty():
            lines = "\n".join(f"- {key}：{value}" for key, value in profile.as_items())
            sections.append(
                "已知用户长期信息（与用户本轮说法冲突时，以本轮输入为准）：\n" + lines
            )

        if working_state is not None:
            slots = [
                f"- {name}：{value}"
                for name in SLOT_FIELDS
                if (value := getattr(working_state, name))
            ]
            if slots:
                sections.append("当前任务状态：\n" + "\n".join(slots))
            if working_state.pending_slots:
                sections.append("仍在等待用户补充：" + "、".join(working_state.pending_slots))

        if summary is not None and summary.summary:
            sections.append("此前对话摘要：\n" + summary.summary)

        return "\n\n".join(sections)

    # -- 内部 -------------------------------------------------------------
    def _tokens(self, *messages: dict | Sequence[dict]) -> int:
        total = 0
        for item in messages:
            if isinstance(item, dict):
                total += estimate_tokens(str(item.get("content") or ""), safety_factor=self.safety_factor)
            else:
                total += self._tokens(*item)
        return total
