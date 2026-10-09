"""上下文构建（雏形）。对应 design.md 第 11 节 / 12.5。

S3 只负责「系统提示 + 历史 + 本轮输入」的组装；动态长期记忆注入、会话摘要与
token 预算将在 S4 补齐（届时此处扩展为完整的 ContextBuilder）。
"""

from __future__ import annotations

from typing import Iterable, Sequence

DEFAULT_SYSTEM_PROMPT = (
    "你是一名智能出行助手。\n"
    "回答用户关于出行、天气与时间的问题；需要事实数据时必须调用工具，不要凭记忆编造。\n"
    "工具返回错误时，向用户说明情况并给出可行的替代建议，不要暴露内部堆栈或调试信息。\n"
    "先给出结论，再补充关键细节，保持简洁。"
)


def build_messages(
    user_input: str,
    *,
    system_prompt: str | None = None,
    history: Sequence[dict] | None = None,
) -> list[dict]:
    """组装本轮对话消息。``history`` 为已存在的消息（不含本轮用户输入）。"""

    messages: list[dict] = [
        {"role": "system", "content": system_prompt or DEFAULT_SYSTEM_PROMPT}
    ]
    if history:
        messages.extend(dict(message) for message in history)
    messages.append({"role": "user", "content": user_input})
    return messages


def extend_history(
    history: Iterable[dict],
    *,
    limit: int | None = None,
) -> list[dict]:
    """截取最近 ``limit`` 条历史（token 预算的简化版，S4 会换成正规实现）。"""

    items = list(history)
    return items[-limit:] if limit else items
