"""内置工具：工具定义、输入输出模型与业务规则（design.md 5 节职责边界）。

工具**不实现网络访问细节**，外部 IO 一律交给 ``providers``。
"""

from wayfinder.tools.builtin.calendar import (
    CalendarInput,
    CalendarResult,
    make_calendar_tool,
)
from wayfinder.tools.builtin.time import (
    ResolveTimeInput,
    ResolvedTimeResult,
    TimeInput,
    TimeResult,
    make_resolve_datetime_tool,
    make_time_tool,
)
from wayfinder.tools.builtin.travel import (
    TravelTimeInput,
    TravelTimeResult,
    make_travel_time_tool,
)
from wayfinder.tools.builtin.weather import (
    WeatherInput,
    WeatherResult,
    make_weather_tool,
)

__all__ = [
    "make_time_tool",
    "TimeInput",
    "TimeResult",
    "make_resolve_datetime_tool",
    "ResolveTimeInput",
    "ResolvedTimeResult",
    "make_weather_tool",
    "WeatherInput",
    "WeatherResult",
    "make_travel_time_tool",
    "TravelTimeInput",
    "TravelTimeResult",
    "make_calendar_tool",
    "CalendarInput",
    "CalendarResult",
]
