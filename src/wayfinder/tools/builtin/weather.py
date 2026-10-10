"""天气工具（design.md 11.5）。外部 IO 由 ``WeatherProvider`` 注入。"""

from __future__ import annotations

from datetime import date
from typing import Any, Callable

from pydantic import BaseModel, Field

from wayfinder.providers.base import ProviderNotFound, WeatherProvider
from wayfinder.schemas.tool import ToolSpec


class WeatherInput(BaseModel):
    city: str = Field(description="城市名称，例如 重庆、北京、上海")
    date: str | None = Field(default=None, description="日期 YYYY-MM-DD；不填表示今天")


class WeatherResult(BaseModel):
    city: str
    date: str
    condition: str
    temperature_c: float
    feels_like_c: float | None = Field(default=None, description="体感温度；预报数据为空")
    temp_min_c: float | None = None
    temp_max_c: float | None = None
    humidity: int
    source: str


def make_weather_tool(
    provider: WeatherProvider,
    *,
    today: Callable[[], str] | None = None,
) -> ToolSpec:
    """``provider`` 实现 ``WeatherProvider`` 协议；``today`` 可注入以便测试。"""

    current_day = today or (lambda: date.today().isoformat())

    def handler(payload: WeatherInput) -> dict[str, Any]:
        target_date = payload.date or current_day()
        data = provider.get_weather(payload.city, target_date)
        if not data:
            # 空结果按「查不到」处理（design.md 9.3 的空结果场景）
            raise ProviderNotFound(f"未找到城市：{payload.city}")
        return dict(data)

    return ToolSpec(
        name="get_weather",
        description="查询指定城市的天气；当天返回实况，未来 3 天返回预报",
        input_model=WeatherInput,
        output_model=WeatherResult,
        handler=handler,
        timeout_s=5.0,
        max_retries=1,
        # 城市对应「目的地」槽位：Plan 前置校验据此判断规划器是否在**猜城市**
        slot_bindings={"city": "destination", "date": "date"},
    )
