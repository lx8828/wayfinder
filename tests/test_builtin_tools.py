"""内置工具测试：时间与天气（design.md 11.5）。"""

from __future__ import annotations

from datetime import datetime, timezone

from first_agent.providers import FakeWeatherProvider
from first_agent.schemas.tool import ToolCall, ToolSpec
from first_agent.tools.builtin import make_time_tool, make_weather_tool
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
