"""内置工具测试：时间、天气、出行耗时与日程（design.md 11.5）。"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from first_agent.providers import (
    FakeCalendarProvider,
    FakeTravelProvider,
    FakeWeatherProvider,
)
from first_agent.schemas.tool import ToolCall, ToolSpec
from first_agent.tools.builtin import (
    make_calendar_tool,
    make_time_tool,
    make_travel_time_tool,
    make_weather_tool,
)
from first_agent.tools.executor import ToolExecutor
from first_agent.tools.registry import ToolRegistry


def run_tool(spec: ToolSpec, arguments: dict):
    registry = ToolRegistry()
    registry.register(spec)
    return ToolExecutor(registry).execute(
        ToolCall(call_id="c1", tool_name=spec.name, arguments=arguments)
    )


# -- 时间 ------------------------------------------------------------------


def test_time_tool_uses_injected_clock():
    fixed = datetime(2026, 10, 10, 8, 30, tzinfo=timezone.utc)
    result = run_tool(make_time_tool(clock=lambda: fixed), {"timezone": "Asia/Shanghai"})

    assert result.ok is True
    assert result.data["iso_time"] == "2026-10-10T16:30:00+08:00"
    assert result.data["timezone"] == "Asia/Shanghai"


def test_time_tool_defaults_to_asia_shanghai():
    fixed = datetime(2026, 10, 10, 0, 0, tzinfo=timezone.utc)
    result = run_tool(make_time_tool(clock=lambda: fixed), {})

    assert result.data["timezone"] == "Asia/Shanghai"
    assert result.data["iso_time"] == "2026-10-10T08:00:00+08:00"


def test_time_tool_rejects_unknown_timezone():
    result = run_tool(make_time_tool(), {"timezone": "Mars/Olympus"})

    assert result.ok is False
    assert result.error_code == "not_found"


# -- 天气 ------------------------------------------------------------------


def test_weather_tool_fills_today_when_date_missing():
    provider = FakeWeatherProvider()
    spec = make_weather_tool(provider, today=lambda: "2026-10-10")

    result = run_tool(spec, {"city": "重庆"})

    assert result.ok is True
    assert result.data["date"] == "2026-10-10"
    assert result.data["city"] == "重庆"
    assert provider.calls == [("重庆", "2026-10-10")]


def test_weather_tool_empty_result_is_not_found():
    provider = FakeWeatherProvider(empty_cities=["空城"])
    result = run_tool(make_weather_tool(provider), {"city": "空城"})

    assert result.ok is False
    assert result.error_code == "not_found"


def test_weather_tool_surfaces_provider_timeout():
    provider = FakeWeatherProvider(failures={"北京": "timeout"})
    result = run_tool(make_weather_tool(provider), {"city": "北京"})

    assert result.ok is False
    assert result.error_code == "timeout"


def test_weather_tool_schema_marks_date_optional():
    spec = make_weather_tool(FakeWeatherProvider())
    params = spec.to_function_schema()["function"]["parameters"]

    assert params["required"] == ["city"]
    assert set(params["properties"]) == {"city", "date"}


# -- 出行耗时：mode 必须是枚举契约（S13 真实运行暴露） --------------------


def test_travel_tool_schema_declares_mode_enum():
    """约束要写进 schema：只在描述里列举取值，模型看不到、也拦不住。"""

    spec = make_travel_time_tool(FakeTravelProvider())
    schema = spec.to_function_schema()["function"]["parameters"]

    assert schema["properties"]["mode"]["enum"] == [
        "walk",
        "bike",
        "bus",
        "subway",
        "car",
        "taxi",
        "train",
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("driving", "car"),
        ("驾车", "car"),
        ("开车", "car"),
        ("打车", "taxi"),
        ("高铁", "train"),
        ("地铁", "subway"),
        ("步行", "walk"),
        ("高铁  ", "train"),
    ],
)
def test_travel_tool_normalizes_common_mode_aliases(raw, expected):
    """模型不会只用枚举里的英文：别名要在契约层归一化，而不是让 provider 报 not_found。"""

    provider = FakeTravelProvider()
    result = run_tool(
        make_travel_time_tool(provider),
        {"origin": "重庆", "destination": "成都", "mode": raw},
    )

    assert result.ok is True
    assert result.data["mode"] == expected
    # provider 收到的是规范值（不是别名）
    assert provider.calls[-1][2] == expected


def test_tool_mode_enum_matches_provider_supported_modes():
    """工具契约的枚举与 provider 的支持集必须一致。

    这次就是这个坑：给工具加了 ``train`` 却忘了给 provider 加，
    于是「合法的方式」在 provider 层变成 ``not_found``（看起来像查不到路线）。
    """

    from typing import get_args

    from first_agent.providers.travel import MODE_SPEED_KMH
    from first_agent.tools.builtin.travel import TravelMode

    assert set(get_args(TravelMode)) == set(MODE_SPEED_KMH)


def test_travel_tool_rejects_unknown_mode_with_legal_values():
    """无法识别的方式要给出**明确可选值**，而不是含糊的 not_found。"""

    result = run_tool(
        make_travel_time_tool(FakeTravelProvider()),
        {"origin": "重庆", "destination": "成都", "mode": "rocket"},
    )

    assert result.ok is False
    assert result.error_code == "invalid_arguments"
    message = result.error_message or ""
    assert "rocket" in message
    assert "car" in message and "train" in message


def test_travel_tool_accepts_declared_mode():
    provider = FakeTravelProvider(routes={("重庆", "成都", "car"): 320})
    result = run_tool(
        make_travel_time_tool(provider),
        {"origin": "重庆", "destination": "成都", "mode": "car"},
    )

    assert result.ok is True
    assert result.data["duration_minutes"] == 320


# -- 日程：日期格式必须是契约（否则会变成静默的错误答案） ----------------


def test_calendar_tool_rejects_non_iso_date():
    """非 ISO 日期曾经 → provider 查不到 → 空列表 → 回答"你没有安排"。

    这是**静默的错误答案**，比报错更危险；现在必须显式拒绝。
    """

    provider = FakeCalendarProvider(events={"u1": {"2026-10-10": ["09:00 晨会"]}})
    result = run_tool(make_calendar_tool(provider), {"user_id": "u1", "date": "今天"})

    assert result.ok is False
    assert result.error_code == "invalid_arguments"
    assert "pattern" in (result.error_message or "")


def test_calendar_tool_accepts_iso_date():
    provider = FakeCalendarProvider(events={"u1": {"2026-10-10": ["09:00 晨会"]}})
    result = run_tool(make_calendar_tool(provider), {"user_id": "u1", "date": "2026-10-10"})

    assert result.ok is True
    assert result.data["events"] == ["09:00 晨会"]
