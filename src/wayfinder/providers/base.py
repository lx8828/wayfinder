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


class ProviderAmbiguousPlace(ProviderNotFound):
    """**信息不够具体**，无法唯一确定地点（如「人民路」「火车站」）。

    与 ``not_found`` 的区别在于"该怎么办"：这类失败**补充信息就能成功**，
    所以运行时应当转成 ``need_info`` 追问用户，而不是报"查不到"就结束。
    继承 ``ProviderNotFound`` 是为了让不区分两者的调用方仍然按"没找到"处理。
    """

    code = "ambiguous_place"


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


@runtime_checkable
class DateTimeProvider(Protocol):
    def resolve(self, expression: str, timezone_name: str = "Asia/Shanghai") -> dict[str, Any]:
        """把「明天下午」这类表达式解析为具体日期，返回 ``ResolvedTimeResult`` 所需字段。"""
        ...


@runtime_checkable
class TravelProvider(Protocol):
    def estimate(self, origin: str, destination: str, mode: str) -> dict[str, Any]:
        """返回 ``TravelTimeResult`` 所需的字段（duration_minutes 等）。"""
        ...
