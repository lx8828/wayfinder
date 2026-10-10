"""S6 新增 provider（时间解析 / 出行耗时 / 本地日程）与工具测试。"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone

import pytest

from wayfinder.core.client import LLMClient
from wayfinder.evals.scripted import ScriptedLLM, json_obj, json_raw
from wayfinder.providers import (
    FakeCalendarProvider,
    FakeDateTimeProvider,
    FakeTravelProvider,
    HeuristicTravelProvider,
    LlmDateTimeProvider,
    LocalCalendarProvider,
)
from wayfinder.providers.base import ProviderNotFound, ProviderUnavailable
from wayfinder.schemas.tool import ToolCall
from wayfinder.tools.builtin import (
    make_calendar_tool,
    make_resolve_datetime_tool,
    make_travel_time_tool,
)
from wayfinder.tools.executor import ToolExecutor
from wayfinder.tools.registry import ToolRegistry

TODAY = date(2026, 10, 10)
FIXED_CLOCK = datetime(2026, 10, 10, 8, 30, tzinfo=timezone.utc)


def run_tool(spec, arguments, *, defaults=None):
    registry = ToolRegistry()
    registry.register(spec)
    return ToolExecutor(registry).execute(
        ToolCall(call_id="c1", tool_name=spec.name, arguments=arguments), defaults=defaults
    )


# -- FakeDateTimeProvider --------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("今天", "2026-10-10"),
        ("明天", "2026-10-11"),
        ("后天", "2026-10-12"),
        ("大后天", "2026-10-13"),
        ("3天后", "2026-10-13"),
        ("3 天后", "2026-10-13"),
        ("2026-12-25", "2026-12-25"),
    ],
)
def test_fake_datetime_rules(expression, expected):
    provider = FakeDateTimeProvider(today=lambda: TODAY)

    assert provider.resolve(expression)["date"] == expected


def test_fake_datetime_handles_time_of_day_words():
    provider = FakeDateTimeProvider(today=lambda: TODAY)

    tomorrow = provider.resolve("明天下午")
    assert tomorrow["date"] == "2026-10-11"
    assert tomorrow["time"] == "14:00"

    assert provider.resolve("今天上午")["time"] == "09:00"
    assert provider.resolve("明天晚上")["time"] == "20:00"

    # 只给时段：默认今天
    afternoon = provider.resolve("下午")
    assert afternoon["date"] == "2026-10-10"
    assert afternoon["time"] == "14:00"


def test_fake_datetime_override_wins():
    provider = FakeDateTimeProvider(
        today=lambda: TODAY, responses={"下周三": {"date": "2026-10-14", "time": "09:00"}}
    )

    data = provider.resolve("下周三")

    assert data["date"] == "2026-10-14"
    assert data["time"] == "09:00"


def test_fake_datetime_unknown_expression_raises_not_found():
    provider = FakeDateTimeProvider(today=lambda: TODAY)

    with pytest.raises(ProviderNotFound):
        provider.resolve("某个奇怪的时刻")


def test_fake_datetime_failure_injection():
    provider = FakeDateTimeProvider(today=lambda: TODAY, failures={"明天": "timeout"})

    with pytest.raises(Exception) as excinfo:
        provider.resolve("明天")

    assert getattr(excinfo.value, "code", None) == "timeout"


def test_fake_datetime_records_calls():
    provider = FakeDateTimeProvider(today=lambda: TODAY)
    provider.resolve("明天", "Asia/Shanghai")

    assert provider.calls == [("明天", "Asia/Shanghai")]


# -- LlmDateTimeProvider ---------------------------------------------------


def make_llm_datetime(script, **kwargs):
    return LlmDateTimeProvider(LLMClient(ScriptedLLM(script)), clock=lambda: FIXED_CLOCK, **kwargs)


def test_llm_datetime_parses_date_and_normalizes_time():
    provider = make_llm_datetime([json_obj({"date": "2026-10-11", "time": "14:00:00"})])

    data = provider.resolve("明天下午")

    assert data["date"] == "2026-10-11"
    assert data["time"] == "14:00"
    assert data["source"] == "llm"


def test_llm_datetime_without_time_returns_none():
    provider = make_llm_datetime([json_obj({"date": "2026-10-11", "time": None})])

    assert provider.resolve("明天")["time"] is None


def test_llm_datetime_rejects_invalid_date():
    provider = make_llm_datetime([json_raw('{"date": "明天"}'), json_raw('{"date": "明天"}')])

    with pytest.raises(ProviderUnavailable, match="不是合法日期"):
        provider.resolve("明天")


def test_llm_datetime_rejects_out_of_window_date():
    """拦截模型幻觉出的离谱日期。"""

    provider = make_llm_datetime([json_obj({"date": "1900-01-01"})])

    with pytest.raises(ProviderUnavailable, match="超出合理范围"):
        provider.resolve("明年")


def test_llm_datetime_rejects_empty_expression():
    provider = make_llm_datetime([])

    with pytest.raises(ProviderUnavailable, match="为空"):
        provider.resolve("   ")


def test_llm_datetime_rejects_unknown_timezone():
    provider = make_llm_datetime([])

    with pytest.raises(ProviderUnavailable, match="未知时区"):
        provider.resolve("明天", "Mars/Olympus")


def test_llm_datetime_wraps_json_failure():
    provider = make_llm_datetime([json_raw("bad"), json_raw("bad")])

    with pytest.raises(ProviderUnavailable, match="解析失败"):
        provider.resolve("明天")


# -- 出行耗时 --------------------------------------------------------------


def test_heuristic_travel_is_deterministic_per_mode():
    provider = HeuristicTravelProvider(distance_km=12.0)

    walk = provider.estimate("家", "公司", "walk")["duration_minutes"]
    car = provider.estimate("家", "公司", "car")["duration_minutes"]

    assert walk == 144  # 12 / 5 * 60
    assert car == 16  # 12 / 45 * 60
    assert walk > car
    assert provider.estimate("家", "公司", "car")["source"] == "heuristic"


def test_heuristic_travel_rejects_unknown_mode():
    with pytest.raises(ProviderNotFound, match="不支持的出行方式"):
        HeuristicTravelProvider().estimate("家", "公司", "火箭")


def test_fake_travel_route_override_and_failure():
    provider = FakeTravelProvider(
        routes={("家", "机场", "taxi"): 42}, failures={"subway": "unavailable"}
    )

    assert provider.estimate("家", "机场", "taxi")["duration_minutes"] == 42
    assert provider.estimate("家", "机场", "taxi")["source"] == "fake"
    with pytest.raises(Exception) as excinfo:
        provider.estimate("家", "机场", "subway")
    assert getattr(excinfo.value, "code", None) == "unavailable"
    assert provider.calls == [
        ("家", "机场", "taxi"),
        ("家", "机场", "taxi"),
        ("家", "机场", "subway"),
    ]


# -- 本地日程 --------------------------------------------------------------


def test_local_calendar_missing_file_yields_empty(tmp_path):
    provider = LocalCalendarProvider(tmp_path / "nope.json")

    assert provider.get_events("u1", "2026-10-11") == {"date": "2026-10-11", "events": []}


def test_local_calendar_reads_events(tmp_path):
    path = tmp_path / "calendar.json"
    path.write_text(
        json.dumps({"u1": {"2026-10-11": ["开会", "健身"]}}, ensure_ascii=False),
        encoding="utf-8",
    )
    provider = LocalCalendarProvider(path)

    assert provider.get_events("u1", "2026-10-11")["events"] == ["开会", "健身"]
    assert provider.get_events("u1", "2026-10-12")["events"] == []
    assert provider.get_events("u2", "2026-10-11")["events"] == []


def test_local_calendar_rejects_broken_json(tmp_path):
    path = tmp_path / "calendar.json"
    path.write_text("{不是 JSON", encoding="utf-8")

    with pytest.raises(ProviderUnavailable, match="无法读取"):
        LocalCalendarProvider(path).get_events("u1", "2026-10-11")


def test_local_calendar_rejects_non_object(tmp_path):
    path = tmp_path / "calendar.json"
    path.write_text("[]", encoding="utf-8")

    with pytest.raises(ProviderUnavailable, match="JSON 对象"):
        LocalCalendarProvider(path).get_events("u1", "2026-10-11")


# -- 工具壳 ----------------------------------------------------------------


def test_resolve_datetime_tool_exposes_date_field():
    """``$steps.<id>.date`` 依赖这个字段名，不能改名。"""

    spec = make_resolve_datetime_tool(FakeDateTimeProvider(today=lambda: TODAY))

    result = run_tool(spec, {"expression": "明天", "timezone": "Asia/Shanghai"})

    assert result.ok is True
    assert result.data["date"] == "2026-10-11"
    assert result.data["iso_datetime"] is None  # 没给时刻就不编造


def test_resolve_datetime_tool_builds_iso_when_time_present():
    spec = make_resolve_datetime_tool(
        FakeDateTimeProvider(today=lambda: TODAY, responses={"明天下午": {"date": "2026-10-11", "time": "14:00"}})
    )

    result = run_tool(spec, {"expression": "明天下午"})

    assert result.data["iso_datetime"] == "2026-10-11T14:00:00"


def test_travel_tool_returns_duration():
    spec = make_travel_time_tool(FakeTravelProvider())

    result = run_tool(spec, {"origin": "家", "destination": "公司", "mode": "car"})

    assert result.ok is True
    assert result.data["duration_minutes"] > 0


def test_calendar_tool_needs_user_id_but_defaults_can_supply_it():
    registry = ToolRegistry()
    registry.register(make_calendar_tool(FakeCalendarProvider()))
    executor = ToolExecutor(registry)
    call = ToolCall(call_id="c", tool_name="get_calendar_events", arguments={"date": "2026-10-11"})

    without = executor.execute(call)
    with_defaults = executor.execute(call, defaults={"user_id": "u1"})

    assert without.error_code == "invalid_arguments"
    assert with_defaults.ok is True
    assert with_defaults.data == {"date": "2026-10-11", "events": []}


def test_explicit_arguments_win_over_defaults():
    registry = ToolRegistry()
    registry.register(make_calendar_tool(FakeCalendarProvider(events={"u1": {"2026-10-11": ["开会"]}})))
    executor = ToolExecutor(registry)
    call = ToolCall(
        call_id="c",
        tool_name="get_calendar_events",
        arguments={"user_id": "u1", "date": "2026-10-11"},
    )

    result = executor.execute(call, defaults={"user_id": "其他用户"})

    assert result.data["events"] == ["开会"]
