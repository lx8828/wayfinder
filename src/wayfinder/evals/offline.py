"""离线夹具装配（design.md 16.4 / 16.5）。

把一条 ``EvalCase`` 装配成一个**独立**的被测运行体：

- ``ScriptedLLM``：固定脚本，不联网、不需要 API Key
- Fake providers：固定天气 / 日程 / 时间解析 / 出行耗时
- 独立 ``:memory:`` SQLite：用例之间不共享任何数据库
- 固定基准日与时钟：让「明天」等相对时间可复现

**脚本编排规则**（``offline.script`` 显式给出时整段替换推导）：

```text
react: [tool(...) * 期望调用] + [text(最终回答)] + [json(记忆抽取)] + [json(裁决) * n]
plan : [json(槽位)] + [json(计划)] + [json(重规划)?] + [text(汇总)] + [json(记忆抽取)] ...
```

``need_info`` 的计划不会走到汇总，因此不追加汇总步骤。
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable

from wayfinder.core.client import LLMClient
from wayfinder.core.db import Database
from wayfinder.core.engine import ToolAgentRuntime
from wayfinder.core.session import SessionStore
from wayfinder.evals.adapters import runtime_engine
from wayfinder.evals.case import (
    EvalCase,
    OfflineFixture,
    effective_max_steps,
    eval_user_id,
)
from wayfinder.evals.runner import Engine
from wayfinder.evals.scripted import (
    ToolCallSpec,
    fail,
    json_obj,
    json_raw,
    text,
    tool,
    tools,
)
from wayfinder.evals.scripted import ScriptedLLM
from wayfinder.memory.service import MemoryService
from wayfinder.memory.store import MemoryStore
from wayfinder.providers import (
    FakeCalendarProvider,
    FakeDateTimeProvider,
    FakeTravelProvider,
    FakeWeatherProvider,
)
from wayfinder.runtime import PlanExecuteEngine, ReactEngine
from wayfinder.tools.builtin import (
    make_calendar_tool,
    make_resolve_datetime_tool,
    make_time_tool,
    make_travel_time_tool,
    make_weather_tool,
)
from wayfinder.tools.registry import ToolRegistry

# 固定基准日：所有相对时间（明天 / 后天）都以它为准
FIXED_TODAY = date(2026, 10, 10)
FIXED_ISO_DATE = FIXED_TODAY.isoformat()
FIXED_CLOCK: Callable[[], datetime] = lambda: datetime(  # noqa: E731
    2026, 10, 10, 8, 30, tzinfo=timezone.utc
)


def fixture_of(case: EvalCase) -> OfflineFixture:
    """取夹具；未声明时返回全默认值。"""

    return case.offline or OfflineFixture()


# -- 脚本 ------------------------------------------------------------------


def _to_step(item: dict[str, Any]) -> Any:
    """把 JSONL 里的紧凑步骤描述转成 ScriptedLLM 步骤。"""

    if "text" in item:
        return text(str(item["text"]))
    if "tool" in item:
        return tool(str(item["tool"]), item.get("arguments") or {}, thought=item.get("thought"))
    if "tools" in item:
        return tools(
            [ToolCallSpec(**spec) for spec in item["tools"]], thought=item.get("thought")
        )
    if "json" in item:
        return json_obj(item["json"])
    if "json_raw" in item:
        return json_raw(str(item["json_raw"]))
    if "fail" in item:
        return fail(TimeoutError(str(item["fail"])))
    raise ValueError(f"未知的离线脚本步骤：{item!r}")


def _react_calls(case: EvalCase) -> list[tuple[str, dict[str, Any]]]:
    """由 ``expected_*`` 推导 react 的工具调用序列。

    夹具只复现**模型能产出的参数**；``user_id`` 这类环境参数由运行时注入，
    因此不出现在期望参数里。
    """

    names = case.expected_tool_sequence or case.expected_tool_set
    return [(name, dict(case.expected_arguments.get(name) or {})) for name in names]


def _default_answer(case: EvalCase) -> str:
    """缺省最终回答：把**答案层**断言里可满足的值拼进去。

    三条规则：

    - 只处理 ``target == "final_answer"`` 的断言——观测 / 槽位 / 错误层的断言与回答文本
      无关，拼进来只会制造噪音（也正因如此，``项目评审会`` 这种断言不该写在回答上）。
    - ``any_of`` 取第一个候选；``equals`` 直接作为整句回答。
    - **不能**把 ``not_contains`` 的值拼进去——那会让自己必然失败；``regex`` 也无法反推文本，
      这两种情况需要用例显式声明 ``final_answer``。
    """

    equals = next(
        (
            assertion.value
            for assertion in case.assertions
            if assertion.target == "final_answer" and assertion.type == "equals"
        ),
        None,
    )
    if equals is not None:
        return equals

    parts: list[str] = []
    for assertion in case.assertions:
        if assertion.target != "final_answer":
            continue
        if assertion.type == "any_of" and assertion.values:
            parts.append(assertion.values[0])
        elif assertion.type == "contains" and assertion.value:
            parts.append(assertion.value)
    return "离线夹具回答：" + "；".join(parts) if parts else "离线夹具回答。"


def _answer(case: EvalCase, fixture: OfflineFixture) -> str:
    return fixture.final_answer or _default_answer(case)


def _engine_steps(case: EvalCase, fixture: OfflineFixture) -> list[Any]:
    if fixture.script is not None:
        return [_to_step(item) for item in fixture.script]

    if case.mode == "plan":
        if fixture.plan is None:
            raise ValueError(f"{case.case_id}：plan 用例必须在 offline.plan 里给出计划")
        steps: list[Any] = [json_obj(fixture.slots), json_obj(fixture.plan)]
        if fixture.replan is not None:
            steps.append(json_obj(fixture.replan))
        if not (fixture.plan.get("need_info") or []):
            steps.append(text(_answer(case, fixture)))
        return steps

    steps = [tool(name, arguments) for name, arguments in _react_calls(case)]
    steps.append(text(_answer(case, fixture)))
    return steps


def _memory_steps(fixture: OfflineFixture) -> list[Any]:
    steps: list[Any] = [json_obj(fixture.memory_extraction or {"memories": []})]
    steps.extend(json_obj(relation) for relation in fixture.memory_relations)
    return steps


def build_script(case: EvalCase) -> list[Any]:
    """该用例的完整脚本（引擎部分 + 后置记忆部分）。"""

    fixture = fixture_of(case)
    return [*_engine_steps(case, fixture), *_memory_steps(fixture)]


# -- 工具与运行时 ----------------------------------------------------------


def _route_key(raw: str) -> tuple[str, str, str]:
    """``"重庆->成都:car"`` → ``("重庆", "成都", "car")``。"""

    route, _, mode = raw.partition(":")
    origin, _, destination = route.partition("->")
    return origin.strip(), destination.strip(), (mode or "car").strip().lower()


def build_registry(case: EvalCase) -> ToolRegistry:
    fixture = fixture_of(case)
    user_id = eval_user_id(case)

    registry = ToolRegistry()
    registry.register(
        make_weather_tool(
            FakeWeatherProvider(fixture.weather, failures=fixture.weather_failures),
            today=lambda: FIXED_ISO_DATE,
        )
    )
    registry.register(make_time_tool(clock=FIXED_CLOCK))
    registry.register(
        make_resolve_datetime_tool(
            FakeDateTimeProvider(today=lambda: FIXED_TODAY, responses=fixture.datetime_responses)
        )
    )
    registry.register(
        make_travel_time_tool(
            FakeTravelProvider(
                routes={
                    _route_key(key): minutes for key, minutes in fixture.travel_routes.items()
                }
            )
        )
    )
    registry.register(
        make_calendar_tool(
            FakeCalendarProvider(
                events={
                    (user_id if key == "$user" else key): by_day
                    for key, by_day in fixture.calendar.items()
                },
                failures={
                    (user_id if key == "$user" else key): mode
                    for key, mode in fixture.calendar_failures.items()
                },
            )
        )
    )
    return registry


def build_runtime(
    case: EvalCase,
    *,
    tracer: Any = None,
    client: LLMClient | None = None,
    registry: ToolRegistry | None = None,
) -> ToolAgentRuntime:
    """为单个用例装配运行时（独立 DB + 固定脚本）。

    ``client`` 可注入真 LLM——「真 LLM 段」复用本函数，只换模型、不换世界
    （天气/日程/时间仍是确定的 Fake，否则波动指标无从解释）。
    """

    client = client or LLMClient(ScriptedLLM(build_script(case)), tracer=tracer)
    registry = registry or build_registry(case)
    db = Database(":memory:")
    memories = MemoryStore(db)
    max_steps = effective_max_steps(case)

    react = ReactEngine(client, registry, max_steps=max_steps, tracer=tracer)
    plan = PlanExecuteEngine(
        client,
        registry,
        max_steps=max_steps,
        fallback_engine=react,
        tracer=tracer,
    )
    return ToolAgentRuntime(
        engine=react,
        plan_engine=plan,
        sessions=SessionStore(db),
        memories=memories,
        memory_service=MemoryService(client, memories, tracer=tracer),
        tracer=tracer,
    )


def offline_engine_factory(case: EvalCase) -> Engine:
    """``EngineFactory``：**每个用例一个全新的运行时**（design.md 16.4）。"""

    return runtime_engine(build_runtime(case))
