"""S6b：超预算熔断与失败恢复（retry / fallback / replan）。

对应 design.md 8.2（replan 一次）、13（Plan 失败降级到 ReAct）、14.1（超预算行为）
与验收项 ``test_run_budget_exceeded``。
"""

from __future__ import annotations

import pytest

from wayfinder.core.budget import (
    BudgetExceeded,
    RunBudget,
    build_partial_answer,
    check_budget,
)
from wayfinder.core.client import LLMClient
from wayfinder.core.db import Database
from wayfinder.core.engine import ToolAgentRuntime
from wayfinder.core.session import SessionResolver, SessionStore
from wayfinder.evals.scripted import ScriptedLLM, json_obj, json_raw, text, tool
from wayfinder.memory.store import MemoryStore
from wayfinder.runtime import PlanExecuteEngine, ReactEngine
from wayfinder.schemas.tool import ToolResult
from tests.plan_helpers import build_engine, make_registry, user_messages


class SteppingClock:
    """假时钟：每次读取前进固定步长（**首次读取作为起点，不前进**）。

    预算检查发生在每次 LLM / 工具调用**之前**，所以用它可以确定性触发熔断，
    避免依赖真实计时（那会变成 flaky 测试）。
    """

    def __init__(self, step_seconds: float) -> None:
        self._step = step_seconds
        self._seconds = 0.0
        self._reads = 0

    def __call__(self) -> float:
        self._reads += 1
        if self._reads > 1:
            self._seconds += self._step
        return self._seconds


BAD_TOOL_PLAN = {
    "need_info": [],
    "steps": [{"id": 0, "tool": "get_traffic", "arguments": {}, "depends_on": []}],
}
GOOD_TIME_PLAN = {
    "need_info": [],
    "steps": [{"id": 0, "tool": "get_current_time", "arguments": {}, "depends_on": []}],
}


def build_react(script, *, budget_ms=60_000, clock=None, tracer=None):
    sdk = ScriptedLLM(script)
    engine = ReactEngine(
        LLMClient(sdk),
        make_registry(),
        budget_ms=budget_ms,
        clock=clock,
        tracer=tracer,
    )
    return engine, sdk


def build_plan_with_fallback(script, *, budget_ms=60_000, clock=None):
    sdk = ScriptedLLM(script)
    client = LLMClient(sdk)
    registry = make_registry()
    engine = PlanExecuteEngine(
        client,
        registry,
        fallback_engine=ReactEngine(client, registry),
        budget_ms=budget_ms,
        clock=clock,
    )
    return engine, sdk


# -- RunBudget 单元 --------------------------------------------------------


def test_run_budget_rejects_non_positive_budget():
    with pytest.raises(ValueError):
        RunBudget(budget_ms=0)


def test_run_budget_tracks_spent_and_remaining():
    budget = RunBudget(budget_ms=2_500, clock=SteppingClock(1.0))  # 构造时读一次作为起点

    # 每次读取时间前进 1 秒，所以两次读取分别是"已用 1 秒"和"已用 2 秒"
    assert budget.spent_ms == 1_000
    assert budget.remaining_ms == 500


def test_run_budget_exhausted_after_enough_reads():
    budget = RunBudget(budget_ms=2_500, clock=SteppingClock(1.0))

    assert budget.exhausted is False  # 已用 1000ms
    assert budget.exhausted is False  # 已用 2000ms
    assert budget.exhausted is True  # 已用 3000ms ≥ 2500ms


def test_run_budget_check_raises_with_stage():
    budget = RunBudget(budget_ms=1_500, clock=SteppingClock(1.0))

    budget.check("first")  # 已用 1000ms < 1500ms
    with pytest.raises(BudgetExceeded) as excinfo:
        budget.check("second")  # 已用 2000ms ≥ 1500ms

    assert excinfo.value.stage == "second"
    assert excinfo.value.spent_ms >= 1_500


def test_check_budget_returns_instead_of_raising():
    budget = RunBudget(budget_ms=1_500, clock=SteppingClock(1.0))

    assert check_budget(budget, "first") is None
    exceeded = check_budget(budget, "second")

    assert exceeded is not None
    assert exceeded.stage == "second"


def test_build_partial_answer_without_observations():
    answer = build_partial_answer([], spent_ms=61_000, budget_ms=60_000, stage="llm_call")

    assert "60 秒" in answer
    assert "没有取得可用的查询结果" in answer


def test_partial_answer_reports_milliseconds_for_sub_second_budget():
    """回归：``--budget-ms 1`` 曾被说成"已达 1 秒"，与实际严重不符。"""

    answer = build_partial_answer([], spent_ms=2, budget_ms=1, stage="llm_call")

    assert "2 毫秒" in answer  # 已用
    assert "1 毫秒" in answer  # 上限
    assert "1 秒" not in answer


def test_build_partial_answer_lists_only_successful_observations():
    results = [
        ToolResult(
            call_id="c1",
            ok=True,
            tool_name="get_weather",
            data={"city": "北京", "condition": "晴"},
        ),
        ToolResult(
            call_id="c2", ok=False, tool_name="get_calendar_events", error_code="timeout"
        ),
    ]

    answer = build_partial_answer(results, spent_ms=61_000, budget_ms=60_000, stage="summary")

    assert "get_weather" in answer
    assert "晴" in answer
    assert "get_calendar_events" not in answer  # 失败结果不算"部分信息"


# -- ReAct 超预算 ----------------------------------------------------------


def test_run_budget_exceeded_stops_before_tool_and_explains():
    """验收项：预算耗尽 → 可解释退出，**不抛异常**、不继续跑。"""

    engine, sdk = build_react(
        [tool("get_weather", {"city": "北京"}), text("北京晴")],
        budget_ms=1_500,
        clock=SteppingClock(1.0),
    )

    result = engine.run("北京天气")

    assert result.status == "budget_exceeded"
    assert "耗时预算" in result.answer
    assert "没有取得可用的查询结果" in result.answer
    assert result.tool_results == []  # 工具确实没被执行
    assert len(sdk.calls) == 1  # 只发起了一次 LLM 调用


def test_budget_break_keeps_message_protocol_valid():
    """熔断时也必须补齐 tool 消息，否则历史回放会被真实 API 拒绝。"""

    engine, _ = build_react(
        [tool("get_weather", {"city": "北京"}), text("北京晴")],
        budget_ms=1_500,
        clock=SteppingClock(1.0),
    )

    result = engine.run("北京天气")

    roles = [message["role"] for message in result.messages]
    assert roles == ["system", "user", "assistant", "tool"]
    assistant = result.messages[-2]
    assert assistant["tool_calls"][0]["id"] == result.messages[-1]["tool_call_id"]


def test_budget_exceeded_returns_partial_results_when_available():
    engine, _ = build_react(
        [tool("get_weather", {"city": "北京"}), text("北京晴")],
        budget_ms=2_500,
        clock=SteppingClock(1.0),
    )

    result = engine.run("北京天气")

    assert result.status == "budget_exceeded"
    assert len(result.tool_results) == 1
    assert result.tool_results[0].ok is True
    assert "get_weather" in result.answer  # 已取得的部分信息被列出
    assert "北京" in result.answer


def test_budget_exceeded_is_traced_with_stage_and_spent():
    class Recorder:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        def log(self, kind, **payload):
            self.events.append((kind, payload))

    recorder = Recorder()
    engine, _ = build_react(
        [tool("get_weather", {"city": "北京"}), text("北京晴")],
        budget_ms=1_500,
        clock=SteppingClock(1.0),
        tracer=recorder,
    )

    engine.run("北京天气")

    _, payload = next(item for item in recorder.events if item[0] == "run_budget_exceeded")
    assert payload["budget_ms"] == 1_500
    assert payload["stage"] == "tool_call"
    assert payload["spent_ms"] >= 1_500


# -- Plan 超预算 -----------------------------------------------------------


def test_plan_budget_exceeded_before_planning():
    engine, sdk = build_engine(
        [json_obj({})], budget_ms=1_500, clock=SteppingClock(1.0)
    )

    result = engine.run_messages(user_messages("明天天气"))

    assert result.status == "budget_exceeded"
    assert "耗时预算" in result.answer
    assert len(sdk.calls) == 1  # 只跑了槽位抽取，之后就被熔断


def test_plan_budget_exceeded_at_summary_still_returns_observations():
    """工具都跑完了但没预算做汇总 → 用确定性兜底文本交付已有 Observation。"""

    engine, _ = build_engine(
        [json_obj({}), json_obj(GOOD_TIME_PLAN), text("现在是 16:30。")],
        budget_ms=3_500,  # 工具能跑完，但轮不到汇总
        clock=SteppingClock(1.0),
    )

    result = engine.run_messages(user_messages("现在几点"))

    assert result.status == "budget_exceeded"
    assert "get_current_time" in result.answer  # 兜底文本包含观测结果
    assert len(result.tool_results) == 1


# -- replan 一次 -----------------------------------------------------------


def test_plan_replans_once_when_first_plan_is_invalid():
    engine, sdk = build_engine(
        [json_obj({}), json_obj(BAD_TOOL_PLAN), json_obj(GOOD_TIME_PLAN), text("现在是 16:30。")]
    )

    result = engine.run_messages(user_messages("现在几点"))

    assert result.status == "success"
    assert result.tool_calls == ["get_current_time"]
    assert result.answer == "现在是 16:30。"
    assert len(sdk.calls) == 4  # 槽位 + 计划 + 重规划 + 汇总


def test_replan_feedback_carries_the_previous_plan_and_problems():
    engine, sdk = build_engine(
        [json_obj({}), json_obj(BAD_TOOL_PLAN), json_obj(GOOD_TIME_PLAN), text("好")]
    )

    engine.run_messages(user_messages("现在几点"))

    feedback = sdk.calls[2]["messages"][-1]["content"]
    assert "上一次计划" in feedback
    assert "unknown_tool" in feedback
    assert "get_traffic" in feedback


def test_plan_gives_up_after_a_single_replan():
    engine, sdk = build_engine([json_obj({}), json_obj(BAD_TOOL_PLAN), json_obj(BAD_TOOL_PLAN)])

    result = engine.run_messages(user_messages("现在几点"))

    assert result.status == "invalid_plan"
    assert result.tool_calls == []
    assert len(sdk.calls) == 3  # 计划 + 一次重规划，不会无限重试


# -- Plan 失败降级到 ReAct -------------------------------------------------


def test_invalid_plan_degrades_to_react():
    engine, _ = build_plan_with_fallback(
        [json_obj({}), json_obj(BAD_TOOL_PLAN), json_obj(BAD_TOOL_PLAN), text("现在是 16:30。")]
    )

    result = engine.run_messages(user_messages("现在几点"))

    assert result.degraded_to == "react"
    assert result.status == "success"
    assert result.answer == "现在是 16:30。"
    assert result.error is None  # 降级成功不该留失败痕迹（否则评测会误判）


def test_plan_json_failure_degrades_to_react():
    engine, _ = build_plan_with_fallback(
        [json_obj({}), json_raw("bad"), json_raw("still bad"), text("直接回答")]
    )

    result = engine.run_messages(user_messages("随便问问"))

    assert result.degraded_to == "react"
    assert result.status == "success"
    assert result.answer == "直接回答"


def test_plan_failure_without_fallback_engine_asks_the_user():
    engine, _ = build_engine([json_obj({}), json_raw("bad"), json_raw("bad")])

    result = engine.run_messages(user_messages("随便问问"))

    assert result.status == "error"
    assert result.degraded_to is None
    assert "再说明一下" in result.answer


def test_degraded_result_exposes_thoughts_for_cli():
    """回归：``--mode plan --show-thought`` 不能因缺少 thoughts 属性而崩溃。"""

    engine, _ = build_plan_with_fallback(
        [
            json_obj({}),
            json_obj(BAD_TOOL_PLAN),
            json_obj(BAD_TOOL_PLAN),
            tool("get_weather", {"city": "北京"}, thought="先查天气"),
            text("北京晴"),
        ]
    )

    result = engine.run_messages(user_messages("现在几点"))

    assert result.degraded_to == "react"
    assert result.thoughts == ["先查天气"]


def test_plan_result_always_exposes_thoughts():
    engine, _ = build_engine(
        [json_obj({}), json_obj(GOOD_TIME_PLAN), text("16:30")]
    )

    result = engine.run_messages(user_messages("现在几点"))

    assert result.thoughts == []  # Plan 自身没有 Thought，但属性必须存在


# -- 门面：预算耗尽后不再写记忆 --------------------------------------------


def test_runtime_skips_memory_when_budget_is_exhausted():
    class CountingMemory:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def remember(self, user_input, *, user_id):
            self.calls.append(user_input)
            raise AssertionError("预算耗尽时不应再调用记忆服务")

    memory = CountingMemory()
    sdk = ScriptedLLM([text("好的")])
    db = Database(":memory:")
    runtime = ToolAgentRuntime(
        engine=ReactEngine(LLMClient(sdk), make_registry()),
        sessions=SessionStore(db),
        memories=MemoryStore(db),
        memory_service=memory,
        resolver=SessionResolver(id_factory=lambda: "s_fixed"),
        budget_ms=1_500,
        clock=SteppingClock(1.0),
    )

    outcome = runtime.chat("我叫张三", user_id="u1", session_id="s1")

    assert outcome.result.status == "success"  # 引擎正常答完
    assert memory.calls == []  # 但预算已耗尽，不再做后置记忆
    assert outcome.budget_spent_ms >= 1_500
