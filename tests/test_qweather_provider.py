"""QWeather 真实 provider 的解析与错误映射测试（用 stub HTTP，不联网）。"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from wayfinder.providers.base import ProviderNotFound, ProviderUnavailable
from wayfinder.providers.weather import QWeatherProvider


class StubResponse:
    def __init__(self, payload, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class StubHTTP:
    def __init__(self, responses) -> None:
        self._responses = list(responses)
        self.requests: list[tuple[str, dict]] = []

    def get(self, url, **kwargs):
        self.requests.append((url, kwargs))
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


GEO = StubResponse({"code": "200", "location": [{"id": "101040100"}]})
NOW = StubResponse(
    {"code": "200", "now": {"text": "晴", "temp": "28", "feelsLike": "30", "humidity": "55"}}
)


def make_provider(responses) -> QWeatherProvider:
    return QWeatherProvider(api_key="k", host="example.com", http_client=StubHTTP(responses))


def test_now_weather_parsing_for_today():
    provider = make_provider([GEO, NOW])
    data = provider.get_weather("重庆", date.today().isoformat())

    assert data["condition"] == "晴"
    assert data["temperature_c"] == 28.0
    assert data["feels_like_c"] == 30.0
    assert data["humidity"] == 55
    assert data["source"] == "qweather-now"


def test_forecast_parsing_for_future_date():
    target = (date.today() + timedelta(days=1)).isoformat()
    provider = make_provider(
        [
            GEO,
            StubResponse(
                {
                    "code": "200",
                    "daily": [
                        {
                            "fxDate": target,
                            "textDay": "多云",
                            "tempMin": "18",
                            "tempMax": "25",
                            "humidity": "60",
                        }
                    ],
                }
            ),
        ]
    )

    data = provider.get_weather("重庆", target)

    assert data["condition"] == "多云"
    assert data["temperature_c"] == 25.0
    assert data["temp_min_c"] == 18.0
    assert data["temp_max_c"] == 25.0
    assert data["source"] == "qweather-3d"


def test_forecast_without_target_date_raises_not_found():
    target = (date.today() + timedelta(days=1)).isoformat()
    provider = make_provider([GEO, StubResponse({"code": "200", "daily": []})])

    with pytest.raises(ProviderNotFound):
        provider.get_weather("重庆", target)


def test_city_lookup_empty_raises_not_found():
    provider = make_provider([StubResponse({"code": "200", "location": []})])

    with pytest.raises(ProviderNotFound):
        provider.get_weather("不存在城", "2026-10-10")


def test_http_error_maps_to_unavailable():
    provider = make_provider([StubResponse({}, status_code=500)])

    with pytest.raises(ProviderUnavailable):
        provider.get_weather("重庆", "2026-10-10")


def test_network_exception_maps_to_unavailable():
    provider = make_provider([RuntimeError("connection reset")])

    with pytest.raises(ProviderUnavailable):
        provider.get_weather("重庆", "2026-10-10")


def test_api_code_204_maps_to_not_found():
    provider = make_provider([StubResponse({"code": "204"})])

    with pytest.raises(ProviderNotFound):
        provider.get_weather("重庆", "2026-10-10")


def test_api_code_403_maps_to_unavailable():
    provider = make_provider([StubResponse({"code": "403"})])

    with pytest.raises(ProviderUnavailable):
        provider.get_weather("重庆", "2026-10-10")


def test_non_json_response_maps_to_unavailable():
    provider = make_provider([StubResponse(ValueError("not json"))])

    with pytest.raises(ProviderUnavailable):
        provider.get_weather("重庆", "2026-10-10")


def test_requires_api_key():
    with pytest.raises(ValueError):
        QWeatherProvider(api_key="")


def test_requests_carry_api_key_header_and_host():
    http = StubHTTP([GEO, NOW])
    provider = QWeatherProvider(api_key="k", host="example.com", http_client=http)
    provider.get_weather("重庆", date.today().isoformat())

    url, kwargs = http.requests[0]
    assert url.startswith("https://example.com/geo/v2/city/lookup")
    assert kwargs["headers"]["X-QW-Api-Key"] == "k"
    assert kwargs["params"] == {"location": "重庆"}
    assert provider.calls == [("重庆", date.today().isoformat())]
