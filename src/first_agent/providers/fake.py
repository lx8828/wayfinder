"""可注入失败的离线 Fake provider。对应 design.md 9.3 的离线测试场景。

支持：正常结果 / 资源不存在 / 超时 / 服务不可用 / 空结果。
"""

from __future__ import annotations

import copy
import re
from datetime import date, timedelta
from typing import Any, Callable, Iterable, Mapping

from first_agent.providers.base import (
    ProviderError,
    ProviderNotFound,
    ProviderTimeout,
    ProviderUnavailable,
)
from first_agent.providers.travel import HeuristicTravelProvider

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


def _validate_failures(failures: Mapping[str, str], *, label: str = "key") -> dict[str, str]:
    for key, mode in failures.items():
        if mode not in FAILURE_TYPES:
            raise ValueError(
                f"未知的失败模式 {mode!r}（{label}={key}），可选：{sorted(FAILURE_TYPES)}"
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
        self.failures = _validate_failures(failures or {}, label="城市")
        self.empty_cities = set(empty_cities)
        self.calls: list[tuple[str, str]] = []

    def get_weather(self, city: str, date: str) -> dict[str, Any]:
        self.calls.append((city, date))

        mode = self.failures.get(city)
        if mode is not None:
            raise FAILURE_TYPES[mode](f"fake weather failure: {city}/{mode}")

        if city in self.empty_cities:
            return {}

        # 部分覆盖必须**合并**默认值：否则夹具只写 condition 时，
        # humidity / feels_like_c 等缺失会让工具的 output_model 校验失败
        # （表现为 invalid_output，而不是夹具写错）
        payload = {**DEFAULT_WEATHER, **self.responses.get(city, {})}
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
        self.failures = _validate_failures(failures or {}, label="user_id")
        self.calls: list[tuple[str, str]] = []

    def get_events(self, user_id: str, date: str) -> dict[str, Any]:
        self.calls.append((user_id, date))

        mode = self.failures.get(user_id)
        if mode is not None:
            raise FAILURE_TYPES[mode](f"fake calendar failure: {user_id}/{mode}")

        day_events = self.events.get(user_id, {}).get(date, [])
        return {"date": date, "events": list(day_events)}


# -- 时间解析 --------------------------------------------------------------

_RELATIVE_DAYS = {"今天": 0, "today": 0, "明天": 1, "tomorrow": 1, "后天": 2, "大后天": 3}
_AFTER_RE = re.compile(r"(\d+)\s*天后")
# 时段词会先被剥离再匹配日期，同时给出一个默认时刻（例如「明天下午」→ 次日 14:00）
_TIME_OF_DAY = {
    "早上": "08:00",
    "上午": "09:00",
    "中午": "12:00",
    "下午": "14:00",
    "傍晚": "18:00",
    "晚上": "20:00",
}


def _time_of_day(text: str) -> str | None:
    for word, moment in _TIME_OF_DAY.items():
        if word in text:
            return moment
    return None


class FakeDateTimeProvider:
    """**规则式**时间解析：确定性，评测里用它而不是让模型自由发挥。

    - 支持 今天/明天/后天/大后天/「N天后」/ ISO 日期
    - ``responses`` 可对特定表达式显式覆盖
    - ``failures`` 按表达式注入失败
    """

    def __init__(
        self,
        *,
        today: Callable[[], date] | None = None,
        responses: Mapping[str, Mapping[str, Any]] | None = None,
        failures: Mapping[str, str] | None = None,
    ) -> None:
        self._today = today or date.today
        self.responses = {key: dict(value) for key, value in (responses or {}).items()}
        self.failures = _validate_failures(failures or {}, label="expression")
        self.calls: list[tuple[str, str]] = []

    def resolve(self, expression: str, timezone_name: str = "Asia/Shanghai") -> dict[str, Any]:
        text = (expression or "").strip()
        self.calls.append((text, timezone_name))

        mode = self.failures.get(text)
        if mode is not None:
            raise FAILURE_TYPES[mode](f"fake datetime failure: {text}/{mode}")

        time_text: str | None = None
        if text in self.responses:
            payload = dict(self.responses[text])
            target = _parse_iso_date(str(payload.get("date", "")))
            if target is None:
                raise ProviderUnavailable(f"fake 覆盖项日期非法：{payload.get('date')!r}")
            raw_time = payload.get("time")
            time_text = str(raw_time) if raw_time else None
        else:
            target = self._rule_date(text)
            if target is None:
                raise ProviderNotFound(f"无法解析时间表达式：{text}")
            time_text = _time_of_day(text)

        return {
            "expression": text,
            "timezone": timezone_name,
            "date": target.isoformat(),
            "time": time_text,
            "source": "fake",
        }

    def _rule_date(self, text: str) -> date | None:
        if not text:
            return None

        cleaned = text
        for word in _TIME_OF_DAY:
            cleaned = cleaned.replace(word, "")
        cleaned = cleaned.strip()

        if not cleaned:
            # 只说了时段（如「下午」）→ 今天
            return self._today() if _time_of_day(text) else None

        iso = _parse_iso_date(cleaned)
        if iso is not None:
            return iso
        if cleaned in _RELATIVE_DAYS:
            return self._today() + timedelta(days=_RELATIVE_DAYS[cleaned])
        match = _AFTER_RE.search(cleaned)
        if match:
            return self._today() + timedelta(days=int(match.group(1)))
        return None


def _parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat((value or "").strip())
    except ValueError:
        return None


# -- 出行耗时 --------------------------------------------------------------


class FakeTravelProvider(HeuristicTravelProvider):
    """在启发式实现之上加**路线覆盖**与**失败注入**；调用会被记录。"""

    def __init__(
        self,
        routes: Mapping[tuple[str, str, str], int] | None = None,
        *,
        distance_km: float | None = None,
        failures: Mapping[str, str] | None = None,
    ) -> None:
        super().__init__(distance_km=distance_km)
        self.routes = dict(routes or {})
        self.failures = _validate_failures(failures or {}, label="mode")
        self.calls: list[tuple[str, str, str]] = []

    def estimate(self, origin: str, destination: str, mode: str) -> dict[str, Any]:
        mode_key = (mode or "").strip().lower()
        self.calls.append((origin, destination, mode_key))

        failure = self.failures.get(f"{origin}->{destination}:{mode_key}") or self.failures.get(
            mode_key
        )
        if failure is not None:
            raise FAILURE_TYPES[failure](f"fake travel failure: {mode_key}/{failure}")

        minutes = self.routes.get((origin, destination, mode_key))
        if minutes is not None:
            return {
                "origin": origin,
                "destination": destination,
                "mode": mode,
                "duration_minutes": int(minutes),
                "source": "fake",
            }

        data = self._estimate(origin, destination, mode)
        data["source"] = "fake"
        return data
