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
    # 只写在描述里的格式要求等于没要求：S13 真实运行里模型传了非 ISO 日期，
    # provider 查不到就返回空列表，最终变成"你明天没有安排"——**静默的错误答案**。
    # 加上 pattern 后，格式不合法会变成显式的 invalid_arguments（且 schema 会带上 pattern）。
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$", description="日期 YYYY-MM-DD")


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
        # 日期对应「日期」槽位：缺日期时应先追问，而不是猜一个日期去查
        slot_bindings={"date": "date"},
        handler=handler,
        timeout_s=5.0,
        max_retries=1,
    )
