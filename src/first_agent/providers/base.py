"""provider 抽象与统一异常。对应 design.md 9.3。

provider 方法返回的是**普通 dict**（未校验），由工具层用 ``output_model`` 校验后
再进入 ``ToolResult.data``。失败一律通过异常表达，便于工具层映射为结构化
``error_code``（design.md 11.2）。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


class ProviderError(Exception):
    """provider 层异常基类。``code`` 会映射为 ``ToolResult.error_code``。"""

    code = "provider_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code


class ProviderTimeout(ProviderError):
    """外部接口超时。"""

    code = "timeout"


class ProviderNotFound(ProviderError):
    """资源不存在（如城市查不到）。"""

    code = "not_found"


class ProviderUnavailable(ProviderError):
    """外部服务不可用（限流、5xx 等）。"""

    code = "unavailable"


@runtime_checkable
class WeatherProvider(Protocol):
    def get_weather(self, city: str, date: str) -> dict[str, Any]:
        """返回 ``WeatherResult`` 所需的字段（city/date/condition/...）。"""
        ...


@runtime_checkable
class CalendarProvider(Protocol):
    def get_events(self, user_id: str, date: str) -> dict[str, Any]:
        """返回 ``CalendarResult`` 所需的字段（date/events）。"""
        ...
