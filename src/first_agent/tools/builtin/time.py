"""时间工具（design.md 11.5）。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

from first_agent.providers.base import ProviderNotFound
from first_agent.schemas.tool import ToolSpec


class TimeInput(BaseModel):
    timezone: str = Field(default="Asia/Shanghai", description="IANA 时区名，如 Asia/Shanghai")


class TimeResult(BaseModel):
    timezone: str
    iso_time: str


def make_time_tool(*, clock: Callable[[], datetime] | None = None, default_timezone: str = "Asia/Shanghai") -> ToolSpec:
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
