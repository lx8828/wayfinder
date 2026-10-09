"""自然语言时间解析（LLM 实现）。对应 design.md 11.5 的 ``resolve_datetime``。

放在 providers 层而不是 tools 层，是为了保持依赖方向 ``tools ──> providers``：
工具只是壳，真正需要 LLM 的解析逻辑在这里。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel

from first_agent.core.client import JSONValidationError, LLMClient
from first_agent.providers.base import ProviderUnavailable

# 允许的解析窗口：拦截模型幻觉出的离谱日期（如 1900-01-01）
WINDOW_DAYS = 366

SYSTEM_PROMPT = """你是一个时间表达式解析器，把自然语言时间解析为具体日期与时刻。

规则：
- 只输出 JSON，不要解释。
- date 用 YYYY-MM-DD，基于给定的“当前时间”计算相对表达（今天/明天/后天/下周三等）。
- time 用 HH:MM（24 小时制）；无法确定时填 null。

只返回 JSON：{"date": "2026-10-11", "time": "14:00"}
"""


class ResolvedTime(BaseModel):
    date: str
    time: str | None = None


class LlmDateTimeProvider:
    """用一次 ``chat_json`` 完成解析；结果做格式与范围校验。"""

    def __init__(
        self,
        client: LLMClient,
        *,
        clock: Callable[[], datetime] | None = None,
        tracer: Any = None,
    ) -> None:
        self.client = client
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.tracer = tracer

    def resolve(self, expression: str, timezone_name: str = "Asia/Shanghai") -> dict[str, Any]:
        text = (expression or "").strip()
        if not text:
            raise ProviderUnavailable("时间表达式为空")

        try:
            zone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ProviderUnavailable(f"未知时区：{timezone_name}") from exc

        now = self.clock().astimezone(zone)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"当前时间：{now.isoformat(timespec='minutes')}\n"
                    f"时区：{timezone_name}\n"
                    f"时间表达式：{text}"
                ),
            },
        ]
        try:
            resolved = self.client.chat_json(messages, ResolvedTime)
        except JSONValidationError as exc:
            raise ProviderUnavailable(f"时间表达式解析失败：{exc}") from exc

        target = _parse_date(resolved.date)
        if target is None:
            raise ProviderUnavailable(f"解析结果不是合法日期：{resolved.date!r}")
        if abs((target - now.date()).days) > WINDOW_DAYS:
            raise ProviderUnavailable(
                f"解析结果超出合理范围（±{WINDOW_DAYS} 天）：{resolved.date!r}"
            )

        return {
            "expression": text,
            "timezone": timezone_name,
            "date": target.isoformat(),
            "time": _normalize_time(resolved.time),
            "source": "llm",
        }


def _parse_date(value: str) -> date | None:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


def _normalize_time(value: str | None) -> str | None:
    if not value:
        return None
    text = value.strip()
    for pattern in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, pattern).strftime("%H:%M")
        except ValueError:
            continue
    return None
