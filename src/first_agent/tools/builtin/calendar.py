"""日程查询工具（design.md 11.5）。

``user_id`` 通常**不出现在 Plan 的 arguments 里**（模型并不知道会话身份），
而是由引擎作为 ambient default 注入 —— 见 ``ToolExecutor.execute(defaults=...)``。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from first_agent.providers.base import CalendarProvider
from first_agent.schemas.tool import ToolSpec


class CalendarInput(BaseModel):
    user_id: str = Field(description="用户标识（通常由会话注入，无需模型提供）")
    date: str = Field(description="日期 YYYY-MM-DD")


class CalendarResult(BaseModel):
    date: str
    events: list[str]


def make_calendar_tool(provider: CalendarProvider) -> ToolSpec:
    def handler(payload: CalendarInput) -> dict:
        return dict(provider.get_events(payload.user_id, payload.date))

    return ToolSpec(
        name="get_calendar_events",
        description="查询指定日期的日程安排",
        input_model=CalendarInput,
        output_model=CalendarResult,
        handler=handler,
        timeout_s=5.0,
        max_retries=1,
    )
