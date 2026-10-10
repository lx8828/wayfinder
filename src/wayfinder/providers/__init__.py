"""外部依赖（IO）层。

依赖方向（design.md 第 6 节）：``tools ──> providers``。
本层只负责取数据，不做业务判断，也不依赖 schemas / tools / memory / runtime。
"""

from wayfinder.providers.base import (
    CalendarProvider,
    DateTimeProvider,
    ProviderAmbiguousPlace,
    ProviderError,
    ProviderNotFound,
    ProviderTimeout,
    ProviderUnavailable,
    TravelProvider,
    WeatherProvider,
)
from wayfinder.providers.amap import AmapTravelProvider
from wayfinder.providers.calendar import LocalCalendarProvider
from wayfinder.providers.datetime import LlmDateTimeProvider
from wayfinder.providers.fake import (
    FakeCalendarProvider,
    FakeDateTimeProvider,
    FakeTravelProvider,
    FakeWeatherProvider,
)
from wayfinder.providers.travel import HeuristicTravelProvider
from wayfinder.providers.weather import QWeatherProvider

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
    "ProviderAmbiguousPlace",
    # 真实实现
    "QWeatherProvider",
    "LlmDateTimeProvider",
    "LocalCalendarProvider",
    "AmapTravelProvider",
    "HeuristicTravelProvider",
    # Fake
    "FakeWeatherProvider",
    "FakeCalendarProvider",
    "FakeDateTimeProvider",
    "FakeTravelProvider",
]
