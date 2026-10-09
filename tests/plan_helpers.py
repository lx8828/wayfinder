"""S6 测试共用装配：一套带 Fake provider 的工具注册表与 Plan 引擎。

（非 test_ 前缀，不会被 pytest 当用例收集。）
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from first_agent.core.budget import DEFAULT_BUDGET_MS
from first_agent.core.client import LLMClient
from first_agent.evals.scripted import ScriptedLLM
from first_agent.providers import (
    FakeCalendarProvider,
    FakeDateTimeProvider,
    FakeTravelProvider,
    FakeWeatherProvider,
)
from first_agent.runtime import PlanExecuteEngine
from first_agent.tools.builtin import (
    make_calendar_tool,
    make_resolve_datetime_tool,
    make_time_tool,
    make_travel_time_tool,
    make_weather_tool,
)
from first_agent.tools.registry import ToolRegistry

FIXED_TODAY = date(2026, 10, 10)
FIXED_CLOCK = lambda: datetime(2026, 10, 10, 8, 30, tzinfo=timezone.utc)  # noqa: E731


def make_registry(*, weather_failures=None, calendar_events=None, datetime_failures=None):
    registry = ToolRegistry()
    registry.register(
        make_weather_tool(
            FakeWeatherProvider(failures=weather_failures or {}),
            today=lambda: FIXED_TODAY.isoformat(),
        )
    )
    registry.register(make_time_tool(clock=FIXED_CLOCK))
    registry.register(
        make_resolve_datetime_tool(
            FakeDateTimeProvider(today=lambda: FIXED_TODAY, failures=datetime_failures or {})
        )
    )
    registry.register(make_travel_time_tool(FakeTravelProvider()))
    registry.register(make_calendar_tool(FakeCalendarProvider(events=calendar_events or {})))
    return registry


def build_engine(
    script,
    *,
    registry=None,
    max_steps=5,
    tracer=None,
    budget_ms=DEFAULT_BUDGET_MS,
    clock=None,
    fallback_engine=None,
):
    sdk = ScriptedLLM(script)
    engine = PlanExecuteEngine(
        LLMClient(sdk),
        registry or make_registry(),
        max_steps=max_steps,
        budget_ms=budget_ms,
        clock=clock,
        fallback_engine=fallback_engine,
        tracer=tracer,
    )
    return engine, sdk


def user_messages(text: str) -> list[dict]:
    return [
        {"role": "system", "content": "你是出行助手"},
        {"role": "user", "content": text},
    ]
