"""可注入失败的离线 Fake provider。对应 design.md 9.3 的离线测试场景。

支持：正常结果 / 城市不存在 / 超时 / 服务不可用 / 空结果。
"""

from __future__ import annotations

import copy
from typing import Any, Iterable, Mapping

from first_agent.providers.base import (
    ProviderError,
    ProviderNotFound,
    ProviderTimeout,
    ProviderUnavailable,
)

FAILURE_TYPES: dict[str, type[ProviderError]] = {
    "timeout": ProviderTimeout,
    "not_found": ProviderNotFound,
    "unavailable": ProviderUnavailable,
}

DEFAULT_WEATHER: dict[str, Any] = {
    "condition": "晴",
    "temperature_c": 28.0,
    "feels_like_c": 30.0,
    "humidity": 55,
}


def _validate_failures(failures: Mapping[str, str]) -> dict[str, str]:
    for city, mode in failures.items():
        if mode not in FAILURE_TYPES:
            raise ValueError(
                f"未知的失败模式 {mode!r}（城市 {city}），可选：{sorted(FAILURE_TYPES)}"
            )
    return dict(failures)


class FakeWeatherProvider:
    """按城市返回预设天气，可注入失败与空结果。"""

    def __init__(
        self,
        responses: Mapping[str, Mapping[str, Any]] | None = None,
        *,
        failures: Mapping[str, str] | None = None,
        empty_cities: Iterable[str] = (),
    ) -> None:
        self.responses = {city: dict(payload) for city, payload in (responses or {}).items()}
        self.failures = _validate_failures(failures or {})
        self.empty_cities = set(empty_cities)
        self.calls: list[tuple[str, str]] = []

    def get_weather(self, city: str, date: str) -> dict[str, Any]:
        self.calls.append((city, date))

        mode = self.failures.get(city)
        if mode is not None:
            raise FAILURE_TYPES[mode](f"fake weather failure: {city}/{mode}")

        if city in self.empty_cities:
            return {}

        payload = self.responses.get(city, DEFAULT_WEATHER)
        result = copy.deepcopy(dict(payload))
        result.setdefault("city", city)
        result.setdefault("date", date)
        result.setdefault("source", "fake")
        return result


class FakeCalendarProvider:
    """按 (user_id, date) 返回日程；``events`` 为 ``{user_id: {date: [事件]}}``。"""

    def __init__(
        self,
        events: Mapping[str, Mapping[str, Iterable[str]]] | None = None,
        *,
        failures: Mapping[str, str] | None = None,
    ) -> None:
        self.events = {
            user: {day: list(items) for day, items in by_day.items()}
            for user, by_day in (events or {}).items()
        }
        self.failures = _validate_failures(failures or {})
        self.calls: list[tuple[str, str]] = []

    def get_events(self, user_id: str, date: str) -> dict[str, Any]:
        self.calls.append((user_id, date))

        mode = self.failures.get(user_id)
        if mode is not None:
            raise FAILURE_TYPES[mode](f"fake calendar failure: {user_id}/{mode}")

        day_events = self.events.get(user_id, {}).get(date, [])
        return {"date": date, "events": list(day_events)}
