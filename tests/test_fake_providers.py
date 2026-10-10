"""Fake provider 测试（design.md 9.3 的离线场景）。"""

from __future__ import annotations

import pytest

from wayfinder.providers import FakeCalendarProvider, FakeWeatherProvider
from wayfinder.providers.base import (
    CalendarProvider,
    ProviderNotFound,
    ProviderTimeout,
    ProviderUnavailable,
    WeatherProvider,
)


def test_default_weather_fills_identity_fields():
    provider = FakeWeatherProvider()
    data = provider.get_weather("重庆", "2026-10-10")

    assert data["city"] == "重庆"
    assert data["date"] == "2026-10-10"
    assert data["source"] == "fake"
    assert provider.calls == [("重庆", "2026-10-10")]


def test_custom_response_is_deep_copied():
    provider = FakeWeatherProvider(responses={"重庆": {"condition": "雨", "temperature_c": 18}})
    first = provider.get_weather("重庆", "d1")
    first["condition"] = "被篡改"
    second = provider.get_weather("重庆", "d1")

    assert second["condition"] == "雨"


def test_failure_injection_maps_to_exception_type():
    provider = FakeWeatherProvider(
        failures={"北京": "timeout", "上海": "not_found", "广州": "unavailable"}
    )

    with pytest.raises(ProviderTimeout):
        provider.get_weather("北京", "d")
    with pytest.raises(ProviderNotFound):
        provider.get_weather("上海", "d")
    with pytest.raises(ProviderUnavailable) as excinfo:
        provider.get_weather("广州", "d")
    assert excinfo.value.code == "unavailable"


def test_empty_result_supported():
    provider = FakeWeatherProvider(empty_cities=["空城"])
    assert provider.get_weather("空城", "d") == {}


def test_unknown_failure_mode_rejected_at_init():
    with pytest.raises(ValueError, match="未知的失败模式"):
        FakeWeatherProvider(failures={"北京": "explode"})


def test_calendar_provider_events_and_defaults():
    provider = FakeCalendarProvider(events={"u1": {"2026-10-11": ["开会", "健身"]}})

    assert provider.get_events("u1", "2026-10-11") == {
        "date": "2026-10-11",
        "events": ["开会", "健身"],
    }
    assert provider.get_events("u1", "2026-10-12") == {"date": "2026-10-12", "events": []}
    assert provider.calls == [("u1", "2026-10-11"), ("u1", "2026-10-12")]


def test_calendar_failure_injection():
    provider = FakeCalendarProvider(failures={"u1": "timeout"})
    with pytest.raises(ProviderTimeout):
        provider.get_events("u1", "d")


def test_fakes_satisfy_provider_protocols():
    assert isinstance(FakeWeatherProvider(), WeatherProvider)
    assert isinstance(FakeCalendarProvider(), CalendarProvider)
