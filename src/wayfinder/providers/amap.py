"""高德地图（Amap）真实 provider：地名 → 坐标 → 路径规划耗时（design.md 9.3 的真实实现）。

为什么要三步：高德的路径规划接口吃的是**经纬度**，而工具契约给的是**地名**
（"解放碑" / "重庆"）。所以先用 **POI 文本搜索**把地名解析成坐标（见 ``_resolve`` 的实测
对照表：**不用地理编码兜底**，它会把"信息不够具体"变成自信的错误答案），再按出行方式选端点：

| mode | 端点 | 说明 |
|---|---|---|
| `car` / `taxi` | `/v3/direction/driving` | 打车耗时 ≈ 驾车耗时（不含等车与上下车时间） |
| `walk` | `/v3/direction/walking` | |
| `bike` | `/v4/direction/bicycling` | ⚠️ v4 响应结构不同：`errcode`/`errmsg`/`data` |
| `bus` / `subway` / `train` | `/v3/direction/transit/integrated` | 公共交通方案；跨城方案里会出现长途汽车/火车 |

设计要点：

- ``http_client`` 可注入（默认 ``httpx`` 模块），因此**解析、错误映射、超时都能离线测试**，
  与 ``QWeatherProvider`` 保持同一套写法。
- 地理编码结果按地名在**进程内缓存**：既省一次往返（每次查询要 3 次 HTTP），也省高德配额。
- 坐标**原样透传** geo 接口返回的 6 位小数，不做任何换算——精度不足是路径规划失败的常见原因。
- ``source`` 标明具体子来源（``amap-driving`` 等），便于区分"真实数据"与"启发式估算"。
"""

from __future__ import annotations

from typing import Any

import httpx

from wayfinder.providers.base import (
    ProviderAmbiguousPlace,
    ProviderNotFound,
    ProviderTimeout,
    ProviderUnavailable,
)

DEFAULT_BASE_URL = "https://restapi.amap.com"

# 出行方式 → 走哪个端点
_DRIVING_MODES = frozenset({"car", "taxi"})
_TRANSIT_MODES = frozenset({"bus", "subway", "train"})
_SUPPORTED_MODES = (
    "car / taxi（驾车）、walk（步行）、bike（骑行）、bus / subway / train（公共交通）"
)

# 高德把「配置问题」和「配额问题」都塞在 info 里：翻成能直接照做的提示。
# 注意区分：INVALID_USER_KEY（Key 本身无效）≠ USERKEY_PLAT_NOMATCH（Key 类型不对），
# 两者的排查方向完全不同，所以必须分开列。
_CONFIG_ERRORS: dict[str, str] = {
    "INVALID_USER_KEY": "Key 无效：确认无手误、Key 未删除、账号已完成实名认证",
    "INVALID_USER_SCODE": "Key 启用了数字签名，需要一并传签名参数",
    "USERKEY_PLAT_NOMATCH": "Key 的平台类型不匹配（控制台里要选「Web 服务」）",
    "SERVICE_NOT_AVAILABLE": "该服务未开通",
    "INSUFFICIENT_PRIVILEGES": "账号无该服务的权限（可能在控制台未开通「路径规划」）",
    "INVALID_PARAMS": "请求参数不合法（多为地名无法编码）",
}
_QUOTA_ERRORS: dict[str, str] = {
    "DAILY_QUERY_OVER_LIMIT": "当日配额已用尽",
    "CUQPS_HAS_EXCEEDED_THE_LIMIT": "并发超限（QPS）",
    "QPS_HAS_EXCEEDED_THE_LIMIT": "并发超限（QPS）",
}
# 这类 info 表示「这个方式到不了 / 超出上限」，重试毫无意义 → 映射为 not_found
_NO_ROUTE_ERRORS: dict[str, str] = {
    "OVER_DIRECTION_RANGE": "超出该出行方式的距离上限（步行/骑行有上限，跨城请用 car 或公共交通）",
    "ENGINE_RESPONSE_DATA_ERROR": "高德引擎无法为该起终点生成路径",
}


def _first(items: Any) -> dict[str, Any] | None:
    """取列表首项；高德在无结果时给空列表或不给该键。"""

    if isinstance(items, list) and items and isinstance(items[0], dict):
        return items[0]
    return None


def _to_float(value: Any) -> float | None:
    """高德的数值字段都是**字符串**（如 ``"13742"``）；解析失败返回 None。"""

    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


class AmapTravelProvider:
    """实现 ``TravelProvider`` 协议：真实路径规划。"""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        timeout_s: float = 3.0,
        http_client: Any = None,
    ) -> None:
        if not api_key:
            raise ValueError("AmapTravelProvider 需要 api_key（AMAP_API_KEY）")
        self.api_key = api_key
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        # 单次请求超时：一次 estimate 最多 3 次请求（2 次地理编码 + 1 次路径规划），
        # 工具层 timeout_s 要留出总量（见 tools/builtin/travel.py）
        self.timeout_s = timeout_s
        self._client = http_client if http_client is not None else httpx
        # 调用记录（评测/测试用来断言"查了谁"），与 Fake provider 的 calls 同构
        self.calls: list[tuple[str, str, str]] = []
        # 地名 → (坐标, adcode, 规范名)：一次查询要 3 次 HTTP，地名重复时不重复付费
        self._resolve_cache: dict[str, tuple[str, str, str]] = {}

    # -- 对外接口 ---------------------------------------------------------
    def estimate(self, origin: str, destination: str, mode: str) -> dict[str, Any]:
        mode_key = (mode or "").strip().lower()
        self.calls.append((origin, destination, mode_key))

        origin_point, origin_city, origin_name = self._resolve(origin)
        destination_point, destination_city, destination_name = self._resolve(destination)

        if mode_key in _DRIVING_MODES:
            payload = self._get(
                "/v3/direction/driving",
                {
                    "origin": origin_point,
                    "destination": destination_point,
                    "extensions": "base",  # 不要逐步导航，省带宽也省 token
                    "strategy": "0",  # 速度优先
                },
            )
            path = _first(payload.get("route", {}).get("paths"))
            source = "amap-driving"
        elif mode_key == "walk":
            payload = self._get(
                "/v3/direction/walking",
                {"origin": origin_point, "destination": destination_point},
            )
            path = _first(payload.get("route", {}).get("paths"))
            source = "amap-walking"
        elif mode_key == "bike":
            payload = self._get(
                "/v4/direction/bicycling",
                {"origin": origin_point, "destination": destination_point},
                family="v4",
            )
            path = _first((payload.get("data") or {}).get("paths"))
            source = "amap-bicycling"
        elif mode_key in _TRANSIT_MODES:
            payload = self._get(
                "/v3/direction/transit/integrated",
                {
                    "origin": origin_point,
                    "destination": destination_point,
                    "city": origin_city,  # 起点的 adcode
                    "cityd": destination_city,  # 终点的 adcode
                    "strategy": "0",
                },
            )
            path = _first(payload.get("route", {}).get("transits"))
            source = "amap-transit"
        else:
            raise ProviderNotFound(
                f"高德不支持出行方式 {mode!r}；可用：{_SUPPORTED_MODES}"
            )

        if path is None:
            raise ProviderNotFound(f"高德没有找到 {origin} → {destination} 的 {mode_key} 路径")

        duration_s = _to_float(path.get("duration"))
        if duration_s is None:
            raise ProviderUnavailable(f"高德返回的路径缺少 duration：{path!r}")

        distance_m = _to_float(path.get("distance"))
        return {
            "origin": origin,
            "destination": destination,
            "mode": mode,
            "duration_minutes": max(1, round(duration_s / 60)),
            "distance_km": round(distance_m / 1000, 1) if distance_m is not None else None,
            # 把解析到的规范地名交出去：地名歧义是真实问题，必须让调用方能核对
            "origin_resolved": origin_name,
            "destination_resolved": destination_name,
            "source": source,
        }

    # -- 内部 -------------------------------------------------------------
    def _resolve(self, place: str) -> tuple[str, str, str]:
        """地名 → ``(坐标 "lon,lat", adcode, 规范名)``；结果缓存（同一进程内）。

        **只用 POI 文本搜索，不退回地理编码**——这是实测数据决定的：

        | 输入 | POI 搜索 | 地理编码（已弃用） |
        |---|---|---|
        | 解放碑 | 解放碑步行街（重庆渝中）✓ | 首条是四川省广元市的住宅区 ✗ |
        | 江北机场 | 重庆江北国际机场 ✓ | 解析到浙江省宁波市江北区 ✗ |
        | 重庆市渝中区人民路 1 号 | 渝中区某营业部 ✓ | 可用 |
        | **人民路** | **无结果** | 陕西省商洛市的某条道路 ✗ |
        | **火车站** | **无结果** | 甘肃省陇南市徽县「火站村」 ✗ |

        结论：POI 有结果时可靠（连完整地址也能落进正确城区）；而 **POI 无结果时，地理编码
        一定会挑一个"道路/村庄级"的随意城市**，把"信息不够具体"变成**自信的错误答案**
        （实测「人民路」被算成 722 公里）。所以这种情况抛 ``ProviderAmbiguousPlace``，
        由运行时**追问用户补充城市**；用户补一句"重庆人民路"就能走通 POI 路径。
        """

        key = place.strip()
        cached = self._resolve_cache.get(key)
        if cached is not None:
            return cached

        poi = _first(self._get("/v3/place/text", {"keywords": key, "offset": "3"}).get("pois"))
        if poi is None or not str(poi.get("location") or "").strip():
            raise ProviderAmbiguousPlace(
                f"无法确定『{place}』的位置：请补充所在城市或更完整的名称"
                "（例如「重庆人民路」）"
            )

        resolved = (
            str(poi["location"]).strip(),
            str(poi.get("adcode") or poi.get("cityname") or "").strip(),
            str(poi.get("name") or key).strip(),
        )
        self._resolve_cache[key] = resolved
        return resolved

    def _get(
        self, path: str, params: dict[str, Any], *, family: str = "v3"
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        query = dict(params)
        # key 必须进 query —— 漏掉它高德会回 "INVALID_USER_KEY"（看起来像 Key 无效，
        # 实际是请求没带 Key）。桩测试只匹配 URL 时抓不到这种错，靠真实调用才发现。
        query["key"] = self.api_key
        if family == "v3":
            query["output"] = "JSON"
        try:
            response = self._client.get(url, params=query, timeout=self.timeout_s)
        except httpx.TimeoutException as exc:
            # 超时是**可重试**的瞬时错误（执行器据此重试一次）
            raise ProviderTimeout(f"高德接口超时（>{self.timeout_s}s）：{path}") from exc
        except Exception as exc:  # 连接失败等
            raise ProviderUnavailable(f"高德接口请求失败：{exc}") from exc

        if response.status_code != 200:
            raise ProviderUnavailable(f"高德接口返回 HTTP {response.status_code}")

        try:
            payload = response.json()
        except Exception as exc:
            raise ProviderUnavailable(f"高德接口返回非 JSON：{exc}") from exc

        if family == "v4":
            # v4（骑行）用 errcode/errmsg，0 表示成功
            code = payload.get("errcode", 0)
            if str(code) not in {"0", ""}:
                raise ProviderUnavailable(
                    f"高德接口错误：errcode={code} {payload.get('errmsg', '')}".strip()
                )
            return payload

        # v3 用 status/info，1 表示成功
        if str(payload.get("status", "")) != "1":
            info = str(payload.get("info", "")).strip()
            no_route = _NO_ROUTE_ERRORS.get(info)
            if no_route is not None:
                # "到不了"不是服务故障：不重试，并说清原因
                raise ProviderNotFound(f"高德无法规划这条路线：{info}（{no_route}）")
            hint = _CONFIG_ERRORS.get(info) or _QUOTA_ERRORS.get(info)
            raise ProviderUnavailable(
                f"高德接口错误：{info or '未知'}{f'（{hint}）' if hint else ''}"
            )
        return payload
