"""外部依赖（IO）层。

依赖方向（design.md 第 6 节）：``tools ──> providers``。
本层只负责取数据，不做业务判断，也不依赖 schemas / tools / memory / runtime。
"""

from first_agent.providers.base import (
    CalendarProvider,
    DateTimeProvider,
    ProviderError,
    ProviderNotFound,
    ProviderTimeout,
    ProviderUnavailable,
    TravelProvider,
    WeatherProvider,
)
from first_agent.providers.calendar import LocalCalendarProvider
from first_agent.providers.datetime import LlmDateTimeProvider
from first_agent.providers.fake import (
    FakeCalendarProvider,
    FakeDateTimeProvider,
    FakeTravelProvider,
    FakeWeatherProvider,
)
from first_agent.providers.travel import HeuristicTravelProvider
from first_agent.providers.weather import QWeatherProvider

__all__ = [
    # 协议
    "WeatherProvider",
    "CalendarProvider",
    "DateTimeProvider",
    "TravelProvider",
    # 异常
    "ProviderError",
    "ProviderTimeout",
    "ProviderNotFound",
    "ProviderUnavailable",
    # 真实实现
    "QWeatherProvider",
    "LlmDateTimeProvider",
    "LocalCalendarProvider",
    "HeuristicTravelProvider",
    # Fake
    "FakeWeatherProvider",
    "FakeCalendarProvider",
    "FakeDateTimeProvider",
    "FakeTravelProvider",
]
