"""外部依赖（IO）层。

依赖方向（design.md 第 6 节）：``tools ──> providers``。
本层只负责取数据，不做业务判断，也不依赖 schemas / tools / memory / runtime。
"""

from first_agent.providers.base import (
    CalendarProvider,
    ProviderError,
    ProviderNotFound,
    ProviderTimeout,
    ProviderUnavailable,
    WeatherProvider,
)
from first_agent.providers.fake import FakeCalendarProvider, FakeWeatherProvider
from first_agent.providers.weather import QWeatherProvider

__all__ = [
    "WeatherProvider",
    "CalendarProvider",
    "ProviderError",
    "ProviderTimeout",
    "ProviderNotFound",
    "ProviderUnavailable",
    "FakeWeatherProvider",
    "FakeCalendarProvider",
    "QWeatherProvider",
]
