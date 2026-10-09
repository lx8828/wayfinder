"""时间工具：当前时间与自然语言时间解析（design.md 11.5）。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from first_agent.providers.base import DateTimeProvider, ProviderNotFound
from first_agent.schemas.tool import ToolSpec


class TimeInput(BaseModel):
    timezone: str = Field(default="Asia/Shanghai", description="IANA 时区名，如 Asia/Shanghai")


class TimeResult(BaseModel):
    timezone: str
    iso_time: str


class ResolveTimeInput(BaseModel):
    expression: str = Field(description="自然语言时间表达式，例如 明天下午、下周三、2026-11-01")
    timezone: str = Field(default="Asia/Shanghai", description="IANA 时区名")


class ResolvedTimeResult(BaseModel):
    expression: str
    timezone: str
    # date 是给 $steps.<id>.date 引用的关键字段（design.md 8.1 示例）
    date: str
    time: str | None = None
    iso_datetime: str | None = None
    source: str


def make_time_tool(
    *,
    clock: Callable[[], datetime] | None = None,
    default_timezone: str = "Asia/Shanghai",
) -> ToolSpec:
    """``clock`` 可注入以支持确定性测试，必须返回**带时区**的 datetime。"""

    now = clock or (lambda: datetime.now(timezone.utc))

    def handler(payload: TimeInput) -> dict:
        try:
            zone = ZoneInfo(payload.timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ProviderNotFound(f"未知时区：{payload.timezone}") from exc
        return {
            "timezone": payload.timezone,
            "iso_time": now().astimezone(zone).isoformat(timespec="seconds"),
        }

    return ToolSpec(
        name="get_current_time",
        description="获取指定时区的当前时间（ISO 8601 格式）",
        input_model=TimeInput,
        output_model=TimeResult,
        handler=handler,
        timeout_s=2.0,
        max_retries=0,
    )


def make_resolve_datetime_tool(provider: DateTimeProvider) -> ToolSpec:
    """``provider`` 内部含一次 LLM 调用，因此超时给得宽、工具层不再叠加重试
    （重试由 ``chat_json`` 状态机负责，design.md 13）。"""

    def handler(payload: ResolveTimeInput) -> dict:
        data = dict(provider.resolve(payload.expression, payload.timezone))
        time_text = data.get("time")
        data["iso_datetime"] = f"{data['date']}T{time_text}:00" if time_text else None
        return data

    return ToolSpec(
        name="resolve_datetime",
        description="把自然语言时间（如“明天下午”）解析为具体日期与时刻",
        input_model=ResolveTimeInput,
        output_model=ResolvedTimeResult,
        handler=handler,
        timeout_s=20.0,
        max_retries=0,
    )
