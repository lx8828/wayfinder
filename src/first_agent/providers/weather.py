"""和风天气（QWeather）真实 provider。对应 design.md 9.3 的真实实现。

- 当天 → ``/v7/weather/now``（实况）
- 未来 3 天 → ``/v7/weather/3d``（预报，按 ``fxDate`` 匹配）

``http_client`` 可注入（默认 ``httpx`` 模块），因此解析与错误映射可以离线测试。
"""

from __future__ import annotations

from datetime import date
from typing import Any

import httpx

from first_agent.providers.base import ProviderNotFound, ProviderUnavailable

DEFAULT_HOST = "devapi.qweather.com"


class QWeatherProvider:
    """实现 ``WeatherProvider`` 协议。"""

    def __init__(
        self,
        *,
        api_key: str,
        host: str | None = None,
        timeout_s: float = 5.0,
        http_client: Any = None,
    ) -> None:
        if not api_key:
            raise ValueError("QWeatherProvider 需要 api_key（QWEATHER_API_KEY）")
        self.api_key = api_key
        self.host = (host or DEFAULT_HOST).strip()
        self.timeout_s = timeout_s
        self._client = http_client if http_client is not None else httpx
        self.calls: list[tuple[str, str]] = []

    # -- 对外接口 ---------------------------------------------------------
    def get_weather(self, city: str, date_str: str) -> dict[str, Any]:
        self.calls.append((city, date_str))
        location_id = self._location_id(city)

        if date_str == date.today().isoformat():
            payload = self._get("/v7/weather/now", {"location": location_id})
            now = payload["now"]
            return {
                "city": city,
                "date": date_str,
                "condition": now["text"],
                "temperature_c": float(now["temp"]),
                "feels_like_c": float(now["feelsLike"]),
                "humidity": int(now["humidity"]),
                "source": "qweather-now",
            }

        payload = self._get("/v7/weather/3d", {"location": location_id})
        for entry in payload.get("daily", []):
            if entry.get("fxDate") == date_str:
                low, high = float(entry["tempMin"]), float(entry["tempMax"])
                return {
                    "city": city,
                    "date": date_str,
                    "condition": entry["textDay"],
                    "temperature_c": high,
                    "temp_min_c": low,
                    "temp_max_c": high,
                    "humidity": int(entry["humidity"]),
                    "source": "qweather-3d",
                }
        raise ProviderNotFound(f"{city} 没有 {date_str} 的预报（仅支持今天起 3 天内）")

    # -- 内部 -------------------------------------------------------------
    def _location_id(self, city: str) -> str:
        payload = self._get("/geo/v2/city/lookup", {"location": city})
        locations = payload.get("location") or []
        if not locations:
            raise ProviderNotFound(f"未找到城市：{city}")
        return str(locations[0]["id"])

    def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        url = f"https://{self.host}{path}"
        try:
            response = self._client.get(
                url,
                headers={"X-QW-Api-Key": self.api_key},
                params=params,
                timeout=self.timeout_s,
            )
        except Exception as exc:  # 网络异常 / 超时
            raise ProviderUnavailable(f"天气接口请求失败：{exc}") from exc

        if response.status_code != 200:
            raise ProviderUnavailable(f"天气接口返回 HTTP {response.status_code}")

        try:
            payload = response.json()
        except Exception as exc:  # 非 JSON 响应
            raise ProviderUnavailable(f"天气接口返回非 JSON：{exc}") from exc

        code = str(payload.get("code", ""))
        if code == "200":
            return payload
        if code in {"204", "404"}:
            raise ProviderNotFound(f"天气接口无数据（code={code}）")
        raise ProviderUnavailable(f"天气接口错误码：{code}")
