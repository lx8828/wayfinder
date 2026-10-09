"""内置工具：工具定义、输入输出模型与业务规则（design.md 5 节职责边界）。

工具**不实现网络访问细节**，外部 IO 一律交给 ``providers``。
"""

from first_agent.tools.builtin.time import TimeInput, TimeResult, make_time_tool
from first_agent.tools.builtin.weather import (
    WeatherInput,
    WeatherResult,
    make_weather_tool,
)

__all__ = [
    "make_time_tool",
    "TimeInput",
    "TimeResult",
    "make_weather_tool",
    "WeatherInput",
    "WeatherResult",
]
