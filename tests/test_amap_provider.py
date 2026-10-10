"""高德 provider：地名解析、模式分流、错误映射与缓存（全部离线，用桩响应）。

真实调用要联网 + 有效 Key，因此这里用**按高德文档构造的桩响应**把链路测死：

- 地名解析**只用 POI 文本搜索**（``/v3/place/text``）：实测地理编码会把「解放碑」首条给到
  四川广元的住宅区、把「江北机场」解析到宁波江北区；而 POI 无结果时地理编码必然给一个
  "道路/村庄级"的随意城市（「人民路」→ 商洛、「火车站」→ 陇南徽县火站村）→ 因此改为追问
- v3 用 ``status``/``info``；v4（骑行）用 ``errcode``/``errmsg``/``data`` —— 两套结构不能混
- 数值字段是**字符串**秒/米，要换算成分钟/公里
- 每个请求都必须带 ``key``（漏掉会得到 ``INVALID_USER_KEY``，像"Key 无效"实为请求缺参）
"""

from __future__ import annotations

import httpx
import pytest

from wayfinder.providers import AmapTravelProvider
from wayfinder.providers.base import (
    ProviderAmbiguousPlace,
    ProviderNotFound,
    ProviderTimeout,
    ProviderUnavailable,
)


def poi(name: str, location: str, adcode: str, cityname: str = "重庆市") -> dict:
    return {"name": name, "location": location, "adcode": adcode, "cityname": cityname}


POI_RESULTS = {
    "重庆": [poi("重庆市", "106.551787,29.562680", "500000")],
    "成都": [poi("成都市", "104.066301,30.572961", "510100", "成都市")],
    "解放碑": [poi("解放碑步行街", "106.575329,29.557253", "500103")],
    "江北机场": [poi("重庆江北国际机场", "106.657653,29.722563", "500112")],
}


def place_payload(keywords: str) -> dict:
    """已知关键字返回真实样例；其余关键字给一个默认坐标，让模式/错误类测试不必关心解析。"""

    pois = POI_RESULTS.get(keywords)
    if pois is None:
        pois = [poi(keywords, "106.550464,29.563761", "500103")]
    return {"status": "1", "info": "OK", "count": str(len(pois)), "pois": pois}


def payload_for(url: str, params: dict) -> object:
    """按 URL 分派桩响应（POI 搜索按 keywords 返回，便于验证解析顺序）。

    ⚠️ 这里**故意不覆盖 ``geocode/geo``**：实现已经不用地理编码兜底了，
    任何意外的请求都会走 ``AssertionError`` 分支暴露出来。
    """

    if "place/text" in url:
        return place_payload(str(params.get("keywords", "")))
    if "direction/driving" in url:
        return DRIVING_OK
    if "direction/walking" in url:
        return WALKING_OK
    if "direction/bicycling" in url:
        return BICYCLING_OK
    if "direction/transit" in url:
        return TRANSIT_OK
    raise AssertionError(f"桩未覆盖的 URL：{url}")


DRIVING_OK = {
    "status": "1",
    "info": "OK",
    "route": {"paths": [{"distance": "314848", "duration": "13742"}]},
}
WALKING_OK = {
    "status": "1",
    "info": "OK",
    "route": {"paths": [{"distance": "820", "duration": "600"}]},
}
BICYCLING_OK = {
    "errcode": 0,
    "errmsg": "OK",
    "data": {"paths": [{"distance": "7500", "duration": "1800"}]},
}
TRANSIT_OK = {
    "status": "1",
    "info": "OK",
    "route": {"distance": "315000", "transits": [{"duration": "10500"}]},
}


class StubResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        return self._payload


class StubClient:
    """记录每次请求（含参数）并按 URL 分派桩响应；可覆盖单条 URL 用于错误注入。"""

    def __init__(self, overrides: dict[str, object] | None = None) -> None:
        self.overrides = overrides or {}
        self.requests: list[tuple[str, dict]] = []

    def get(self, url: str, params: dict | None = None, timeout: float | None = None):
        recorded = dict(params or {})
        self.requests.append((url, recorded))
        for fragment, payload in self.overrides.items():
            if fragment in url:
                if isinstance(payload, Exception):
                    raise payload
                return payload if isinstance(payload, StubResponse) else StubResponse(payload)
        return StubResponse(payload_for(url, recorded))  # type: ignore[arg-type]


def build(overrides: dict[str, object] | None = None, **kwargs) -> tuple[AmapTravelProvider, StubClient]:
    client = StubClient(overrides)
    provider = AmapTravelProvider(api_key="k", http_client=client, **kwargs)
    return provider, client


def urls(client: StubClient) -> list[str]:
    return [url.rsplit("/", 2)[-2] + "/" + url.rsplit("/", 1)[-1] for url, _ in client.requests]


# -- 地名解析 --------------------------------------------------------------


def test_place_is_resolved_with_poi_search_only():
    """解析只走 POI 文本搜索：实测地理编码会把手边地标解析到别的城市。"""

    provider, client = build()

    provider.estimate("解放碑", "江北机场", "car")

    assert "place/text" in client.requests[0][0]
    assert client.requests[0][1]["keywords"] == "解放碑"
    # 全程不出现地理编码请求（实现已弃用它，桩也没覆盖 → 出现即报错）
    assert all("geocode" not in url for url, _ in client.requests)


def test_poi_miss_asks_for_more_information_instead_of_guessing():
    """POI 无结果时**不能**退回地理编码：那会给一个"道路/村庄级"的随意城市。

    实测：地理编码把「人民路」解析成陕西省商洛市的某条道路（算出 722 公里），
    把「火车站」解析成甘肃省陇南市徽县「火站村」——"信息不够具体"被变成自信的错答。
    """

    provider, client = build({"place/text": {"status": "1", "info": "OK", "pois": []}})

    with pytest.raises(ProviderAmbiguousPlace, match="重庆人民路"):
        provider.estimate("人民路", "观音桥", "car")

    # 没有退路可走：除了那次 POI 搜索，一个额外请求都没发
    assert len(client.requests) == 1
    assert "geocode" not in client.requests[0][0]


def test_resolved_names_are_returned_for_verification():
    """解析出的规范名要交出来：地名歧义必须可核对。"""

    provider, _ = build()

    result = provider.estimate("解放碑", "江北机场", "car")

    assert result["origin_resolved"] == "解放碑步行街"
    assert result["destination_resolved"] == "重庆江北国际机场"


def test_resolution_is_cached_within_the_process():
    """一次 estimate 要 3 次 HTTP，地名重复出现时不该重复付费。"""

    provider, client = build()

    provider.estimate("重庆", "成都", "car")
    first = len([url for url, _ in client.requests if "place/text" in url])
    provider.estimate("重庆", "成都", "car")
    second = len([url for url, _ in client.requests if "place/text" in url])

    assert first == 2  # 起点 + 终点
    assert second == 2  # 第二次全部命中缓存


def test_unresolvable_place_says_information_is_insufficient():
    """两个解析器都没结果时，**不是**"此地不存在"，而是信息不够具体（如「人民路」）。

    这类失败补一句城市就能成功，所以要用专门的错误码交给运行时去追问。
    """

    provider, _ = build({"place/text": {"status": "1", "info": "OK", "pois": []}})

    with pytest.raises(ProviderAmbiguousPlace) as excinfo:
        provider.estimate("人民路", "成都", "car")

    assert excinfo.value.code == "ambiguous_place"
    # 仍属于 not_found 家族：不区分两者的调用方照旧按"没找到"处理
    assert isinstance(excinfo.value, ProviderNotFound)
    assert "重庆人民路" in str(excinfo.value)  # 给用户一个可照做的例子


def test_poi_without_location_counts_as_unresolved():
    """POI 命中但没有坐标 → 不能算解析成功，同样要追问。"""

    broken = {"status": "1", "info": "OK", "pois": [{"name": "缺坐标的 POI"}]}
    provider, _ = build({"place/text": broken})

    with pytest.raises(ProviderAmbiguousPlace):
        provider.estimate("A", "B", "car")


# -- 模式分流 --------------------------------------------------------------


def test_driving_converts_seconds_and_meters():
    provider, _ = build()

    result = provider.estimate("重庆", "成都", "car")

    assert result["duration_minutes"] == 229  # 13742 秒
    assert result["distance_km"] == 314.8
    assert result["source"] == "amap-driving"


def test_walking_uses_walking_endpoint():
    provider, client = build()

    result = provider.estimate("A", "B", "walk")

    assert result["duration_minutes"] == 10
    assert result["source"] == "amap-walking"
    assert any("direction/walking" in url for url, _ in client.requests)


def test_bicycling_uses_v4_envelope():
    """v4（骑行）是 ``errcode``/``data.paths``，不能用 v3 的 ``status``/``route.paths`` 解析。"""

    provider, _ = build()

    result = provider.estimate("A", "B", "bike")

    assert result["duration_minutes"] == 30
    assert result["source"] == "amap-bicycling"


@pytest.mark.parametrize("mode", ["bus", "subway", "train"])
def test_public_transport_uses_transit_with_adcodes(mode):
    """公交/地铁/火车都走公交方案，且必须带上起终点的 adcode。"""

    provider, client = build()

    result = provider.estimate("重庆", "成都", mode)

    assert result["duration_minutes"] == 175  # 10500 秒
    assert result["source"] == "amap-transit"
    transit_params = [p for url, p in client.requests if "transit" in url][-1]
    assert transit_params["city"] == "500000" and transit_params["cityd"] == "510100"


def test_taxi_shares_the_driving_endpoint():
    provider, client = build()

    assert provider.estimate("重庆", "成都", "taxi")["source"] == "amap-driving"
    assert any("direction/driving" in url for url, _ in client.requests)


def test_unsupported_mode_lists_alternatives():
    provider, _ = build()

    with pytest.raises(ProviderNotFound, match="car / taxi"):
        provider.estimate("A", "B", "rocket")


# -- 错误映射 --------------------------------------------------------------


@pytest.mark.parametrize(
    ("info", "fragment"),
    [
        ("INVALID_USER_KEY", "实名认证"),
        ("USERKEY_PLAT_NOMATCH", "Web 服务"),
        ("INVALID_USER_SCODE", "数字签名"),
        ("DAILY_QUERY_OVER_LIMIT", "配额"),
    ],
)
def test_amap_error_info_becomes_actionable_message(info, fragment):
    payload = {"status": "0", "info": info}
    provider, _ = build({"place/text": payload})

    with pytest.raises(ProviderUnavailable) as excinfo:
        provider.estimate("A", "B", "car")

    assert info in str(excinfo.value)  # 保留高德原话
    assert fragment in str(excinfo.value)  # 附上可照做的提示
    assert excinfo.value.code == "unavailable"


def test_out_of_range_route_is_not_found_not_unavailable():
    """步行/骑行超距离上限（OVER_DIRECTION_RANGE）不是服务故障，重试没有意义。"""

    provider, _ = build({"direction/walking": {"status": "0", "info": "OVER_DIRECTION_RANGE"}})

    with pytest.raises(ProviderNotFound) as excinfo:
        provider.estimate("A", "B", "walk")

    assert "距离上限" in str(excinfo.value)


def test_unknown_error_info_is_passed_through():
    provider, _ = build({"place/text": {"status": "0", "info": "SOMETHING_NEW"}})

    with pytest.raises(ProviderUnavailable, match="SOMETHING_NEW"):
        provider.estimate("A", "B", "car")


def test_http_error_is_unavailable():
    provider, _ = build({"place/text": StubResponse({}, status_code=502)})

    with pytest.raises(ProviderUnavailable, match="HTTP 502"):
        provider.estimate("A", "B", "car")


def test_timeout_is_retryable_timeout():
    provider, _ = build({"place/text": httpx.TimeoutException("boom")})

    with pytest.raises(ProviderTimeout) as excinfo:
        provider.estimate("A", "B", "car")

    assert excinfo.value.code == "timeout"


def test_connection_error_is_unavailable():
    provider, _ = build({"place/text": httpx.ConnectError("no net")})

    with pytest.raises(ProviderUnavailable, match="请求失败"):
        provider.estimate("A", "B", "car")


def test_v4_error_envelope_is_detected():
    provider, _ = build(
        {"direction/bicycling": {"errcode": 20000, "errmsg": "ENGINE_RESPONSE_DATA_ERROR"}}
    )

    with pytest.raises(ProviderUnavailable, match="ENGINE_RESPONSE_DATA_ERROR"):
        provider.estimate("A", "B", "bike")


def test_empty_route_is_not_found():
    provider, _ = build({"direction/driving": {"status": "1", "info": "OK", "route": {"paths": []}}})

    with pytest.raises(ProviderNotFound, match="没有找到"):
        provider.estimate("A", "B", "car")


def test_route_without_duration_is_unavailable():
    provider, _ = build(
        {"direction/driving": {"status": "1", "info": "OK", "route": {"paths": [{"distance": "100"}]}}}
    )

    with pytest.raises(ProviderUnavailable, match="duration"):
        provider.estimate("A", "B", "car")


def test_missing_api_key_is_rejected_early():
    with pytest.raises(ValueError, match="AMAP_API_KEY"):
        AmapTravelProvider(api_key="")


# -- 请求参数（桩测试最容易漏掉的一类） ------------------------------------


def test_every_request_carries_the_api_key():
    """回归：``_get`` 曾经漏传 ``key``，高德回 ``INVALID_USER_KEY``（像 Key 无效）。

    桩响应只按 URL 分派，因此这类"参数缺失"必须**显式断言参数**才能挡住——
    当时正是靠真实调用才发现的。
    """

    provider, client = build()

    provider.estimate("重庆", "成都", "car")

    assert client.requests, "应当发出请求"
    assert all(params.get("key") == "k" for _, params in client.requests)


def test_v3_requests_ask_for_json_output():
    provider, client = build()

    provider.estimate("重庆", "成都", "car")

    assert all(params.get("output") == "JSON" for _, params in client.requests)


# -- 工具层集成 ------------------------------------------------------------


def test_tool_integration_carries_real_fields_through_the_contract():
    """工具层契约：距离 / 解析名 / source 要能通过 output_model 校验并进 ToolResult。"""

    from wayfinder.schemas.tool import ToolCall
    from wayfinder.tools.builtin import make_travel_time_tool
    from wayfinder.tools.executor import ToolExecutor
    from wayfinder.tools.registry import ToolRegistry

    provider, _ = build()
    registry = ToolRegistry()
    registry.register(make_travel_time_tool(provider))

    result = ToolExecutor(registry).execute(
        ToolCall(
            call_id="c1",
            tool_name="estimate_travel_time",
            arguments={"origin": "解放碑", "destination": "江北机场", "mode": "开车"},
        )
    )

    assert result.ok is True
    assert result.data["duration_minutes"] == 229
    assert result.data["distance_km"] == 314.8
    assert result.data["origin_resolved"] == "解放碑步行街"
    assert result.data["destination_resolved"] == "重庆江北国际机场"
    assert result.data["source"] == "amap-driving"
    assert result.data["mode"] == "car"  # 别名在契约层已归一化


# -- 不确定就问：provider → 执行器 → Plan 引擎（R30） ---------------------


EMPTY_RESOLUTION = {
    "place/text": {"status": "1", "info": "OK", "pois": []},
}


def _registry_with(provider):
    """只注册本用例用到的工具（计划里出现的每个工具都必须在注册表里，否则会算 unknown_tool）。"""

    from wayfinder.tools.builtin import make_time_tool, make_travel_time_tool
    from wayfinder.tools.registry import ToolRegistry

    registry = ToolRegistry()
    registry.register(make_travel_time_tool(provider))
    registry.register(make_time_tool())
    return registry


def test_ambiguous_place_is_not_retried():
    """补一句城市就能成功的事，重试同样的参数毫无意义（只调一次）。"""

    from wayfinder.schemas.tool import ToolCall
    from wayfinder.tools.executor import ToolExecutor

    provider, _ = build(dict(EMPTY_RESOLUTION))
    executor = ToolExecutor(_registry_with(provider))

    result = executor.execute(
        ToolCall(
            call_id="c1",
            tool_name="estimate_travel_time",
            arguments={"origin": "人民路", "destination": "观音桥", "mode": "car"},
        )
    )

    assert result.error_code == "ambiguous_place"
    assert len(provider.calls) == 1
    assert "重庆人民路" in (result.error_message or "")


def test_plan_engine_turns_ambiguous_place_into_a_follow_up_question():
    """Plan 引擎要把「信息不够具体」转成**追问用户**，而不是报"查不到"或继续跑。"""

    from wayfinder.evals.scripted import json_obj
    from tests.plan_helpers import build_engine, user_messages

    provider, _ = build(dict(EMPTY_RESOLUTION))
    plan = {
        "need_info": [],
        "steps": [
            {
                "id": 0,
                "tool": "estimate_travel_time",
                "arguments": {"origin": "人民路", "destination": "观音桥", "mode": "car"},
                "depends_on": [],
            },
            # 独立的第二步：一旦追问，就不该再执行它（用户补充的话可能改变整个计划）
            {"id": 1, "tool": "get_current_time", "arguments": {}, "depends_on": []},
        ],
    }
    engine, sdk = build_engine(
        [json_obj({}), json_obj(plan)], registry=_registry_with(provider)
    )

    result = engine.run_messages(user_messages("从人民路到观音桥开车要多久？"))

    assert result.status == "need_info"
    assert result.need_info == ["origin"]  # 从失败参数里推断出该补哪个槽位
    assert "请补充所在城市" in result.answer  # 回答就是一句可操作的追问
    assert result.tool_calls == ["estimate_travel_time"]  # 第二步没被执行
    assert len(sdk.calls) == 2  # 槽位 + 计划：没有再为汇总多花一次 LLM 调用
