"""Plan-Execute 引擎端到端测试（design.md 第 8 节）。"""

from __future__ import annotations

from pathlib import Path

import wayfinder.evals as evals_pkg
from wayfinder.core.client import LLMClient
from wayfinder.core.db import Database
from wayfinder.core.engine import ToolAgentRuntime
from wayfinder.core.session import SessionResolver, SessionStore
from wayfinder.evals import load_cases, run_case
from wayfinder.evals.adapters import runtime_engine
from wayfinder.evals.scripted import ScriptedLLM, fail, json_obj, json_raw, text
from wayfinder.memory.store import MemoryStore
from wayfinder.runtime import PlanExecuteEngine, ReactEngine
from wayfinder.schemas.memory import MemoryCandidate
from wayfinder.schemas.session import WorkingState
from tests.plan_helpers import build_engine, make_registry, user_messages

CASES_PATH = Path(evals_pkg.__file__).parent / "cases.jsonl"


# -- 依赖链与执行 ----------------------------------------------------------


def test_dependency_chain_resolves_reference_values():
    engine, _ = build_engine(
        [
            json_obj({"date": "明天", "purpose": "出门"}),
            json_obj(
                {
                    "need_info": [],
                    "reason": "先解析时间再查天气",
                    "steps": [
                        {
                            "id": 0,
                            "tool": "resolve_datetime",
                            "arguments": {"expression": "明天"},
                            "depends_on": [],
                        },
                        {
                            "id": 1,
                            "tool": "get_weather",
                            "arguments": {"city": "重庆", "date": "$steps.0.date"},
                            "depends_on": [0],
                        },
                    ],
                }
            ),
            text("明天重庆多云，适合出门。"),
        ]
    )

    # 输入里必须出现「重庆」：规划前置校验不允许计划填一个凭空出现的城市（R26）
    result = engine.run_messages(user_messages("明天在重庆出门合适吗"))

    assert result.status == "success"
    assert result.tool_calls == ["resolve_datetime", "get_weather"]
    assert result.arguments["get_weather"] == [{"city": "重庆", "date": "2026-10-11"}]
    assert result.answer == "明天重庆多云，适合出门。"
    assert result.steps == 2


def test_execution_follows_topological_order_not_list_order():
    engine, _ = build_engine(
        [
            json_obj({}),
            json_obj(
                {
                    "need_info": [],
                    "steps": [
                        # 依赖者写在前面
                        {
                            "id": 1,
                            "tool": "get_weather",
                            "arguments": {"city": "重庆", "date": "$steps.0.date"},
                            "depends_on": [0],
                        },
                        {
                            "id": 0,
                            "tool": "resolve_datetime",
                            "arguments": {"expression": "明天"},
                            "depends_on": [],
                        },
                    ],
                }
            ),
            text("多云。"),
        ]
    )

    result = engine.run_messages(user_messages("明天重庆天气"))

    assert result.tool_calls == ["resolve_datetime", "get_weather"]
    assert [trace["id"] for trace in result.step_traces] == [0, 1]


def test_list_order_does_not_affect_the_result():
    """属性：steps 的书写顺序不影响执行结果（``depends_on`` 才是唯一顺序来源）。"""

    steps_forward = [
        {"id": 0, "tool": "resolve_datetime", "arguments": {"expression": "明天"}, "depends_on": []},
        {
            "id": 1,
            "tool": "get_weather",
            "arguments": {"city": "重庆", "date": "$steps.0.date"},
            "depends_on": [0],
        },
        {"id": 2, "tool": "get_current_time", "arguments": {}, "depends_on": []},
    ]
    steps_reversed = list(reversed(steps_forward))

    def run_with(steps):
        engine, _ = build_engine(
            [
                json_obj({}),
                json_obj({"need_info": [], "steps": steps}),
                text("好了。"),
            ]
        )
        return engine.run_messages(user_messages("明天重庆天气和现在几点"))

    forward = run_with(steps_forward)
    backward = run_with(steps_reversed)

    assert forward.status == backward.status == "success"
    assert forward.tool_calls == backward.tool_calls == [
        "resolve_datetime",
        "get_current_time",
        "get_weather",
    ]
    assert forward.arguments == backward.arguments
    assert [trace["id"] for trace in forward.step_traces] == [
        trace["id"] for trace in backward.step_traces
    ]


def test_ambient_user_id_is_injected_into_tools():
    """Plan 里不需要（也不应该）让模型编出 user_id。"""

    registry = make_registry(calendar_events={"u1": {"2026-10-11": ["开会"]}})
    engine, _ = build_engine(
        [
            json_obj({"date": "明天"}),
            json_obj(
                {
                    "need_info": [],
                    "steps": [
                        {
                            "id": 0,
                            "tool": "resolve_datetime",
                            "arguments": {"expression": "明天"},
                            "depends_on": [],
                        },
                        {
                            "id": 1,
                            "tool": "get_calendar_events",
                            "arguments": {"date": "$steps.0.date"},
                            "depends_on": [0],
                        },
                    ],
                }
            ),
            text("明天有一场会。"),
        ],
        registry=registry,
    )

    state = WorkingState(user_id="u1", session_id="s1")
    result = engine.run_messages(user_messages("明天有什么安排"), working_state=state)

    assert result.status == "success"
    assert result.tool_results[1].data == {"date": "2026-10-11", "events": ["开会"]}


# -- 多轮追问 --------------------------------------------------------------


def test_need_info_sets_pending_slots_and_asks():
    engine, _ = build_engine(
        [
            json_obj({"purpose": "看电影"}),
            json_obj({"need_info": ["目的地"], "steps": [], "reason": "不知道去哪"}),
        ]
    )
    state = WorkingState(user_id="u1", session_id="s1")

    result = engine.run_messages(user_messages("我想去看电影"), working_state=state)

    assert result.status == "need_info"
    # need_info 是结构化契约：模型写「目的地」也会归一化成规范槽位名
    assert result.need_info == ["destination"]
    # 而回答面向用户，用中文
    assert "目的地" in result.answer
    assert result.tool_calls == []
    # 状态被原地更新，供上层持久化
    assert state.pending_slots == ["destination"]
    assert state.purpose == "看电影"


def test_slots_merge_across_turns_without_resetting():
    """design 8.3：用户只补充一个槽位时，必须合并旧状态。"""

    engine, _ = build_engine(
        [
            json_obj({"destination": "重庆"}),
            json_obj(
                {
                    "need_info": [],
                    "steps": [
                        {"id": 0, "tool": "get_current_time", "arguments": {}, "depends_on": []}
                    ],
                }
            ),
            text("现在是 16:30。"),
        ]
    )
    state = WorkingState(
        user_id="u1",
        session_id="s1",
        purpose="看电影",
        date="明天下午",
        pending_slots=["目的地"],
    )

    result = engine.run_messages(user_messages("重庆"), working_state=state)

    assert result.status == "success"
    assert state.destination == "重庆"
    assert state.purpose == "看电影"  # 旧槽位保留
    assert state.date == "明天下午"
    assert state.pending_slots == []  # 追问已补齐


# -- 计划被拒与失败降级 ----------------------------------------------------


BAD_TOOL_PLAN = {
    "need_info": [],
    "steps": [{"id": 0, "tool": "get_traffic", "arguments": {}, "depends_on": []}],
}

CYCLIC_PLAN = {
    "need_info": [],
    "steps": [
        {
            "id": 0,
            "tool": "get_weather",
            "arguments": {"city": "重庆", "date": "$steps.1.date"},
            "depends_on": [1],
        },
        {
            "id": 1,
            "tool": "resolve_datetime",
            "arguments": {"expression": "明天"},
            "depends_on": [0],
        },
    ],
}


def test_unknown_tool_makes_plan_invalid_and_executes_nothing():
    """重规划后仍非法 → 拒绝执行（脚本第 3 步即重规划结果）。"""

    engine, sdk = build_engine([json_obj({}), json_obj(BAD_TOOL_PLAN), json_obj(BAD_TOOL_PLAN)])

    result = engine.run_messages(user_messages("路况怎么样"))

    assert result.status == "invalid_plan"
    assert result.tool_calls == []
    assert [problem.kind for problem in result.plan_problems] == ["unknown_tool"]
    assert len(sdk.calls) == 3  # 槽位 + 计划 + 一次重规划


def test_cyclic_plan_is_rejected():
    engine, _ = build_engine([json_obj({}), json_obj(CYCLIC_PLAN), json_obj(CYCLIC_PLAN)])

    result = engine.run_messages(user_messages("明天天气"))

    assert result.status == "invalid_plan"
    assert "cycle" in [problem.kind for problem in result.plan_problems]


def test_plan_exceeding_max_steps_is_rejected():
    steps = [
        {"id": index, "tool": "get_current_time", "arguments": {}, "depends_on": []}
        for index in range(4)
    ]
    oversized = {"need_info": [], "steps": steps}
    engine, _ = build_engine(
        [json_obj({}), json_obj(oversized), json_obj(oversized)], max_steps=3
    )

    result = engine.run_messages(user_messages("现在几点"))

    assert result.status == "invalid_plan"
    assert "too_many_steps" in [problem.kind for problem in result.plan_problems]


def test_plan_generation_failure_is_reported():
    engine, _ = build_engine([json_obj({}), json_raw("bad"), json_raw("still bad")])

    result = engine.run_messages(user_messages("随便"))

    assert result.status == "error"
    assert "规划失败" in result.error


def test_tool_failure_is_passed_to_the_summary():
    registry = make_registry(weather_failures={"重庆": "unavailable"})
    engine, _ = build_engine(
        [
            json_obj({"destination": "重庆"}),
            json_obj(
                {
                    "need_info": [],
                    "steps": [
                        {
                            "id": 0,
                            "tool": "get_weather",
                            "arguments": {"city": "重庆"},
                            "depends_on": [],
                        }
                    ],
                }
            ),
            text("抱歉，暂时查不到重庆的天气。"),
        ],
        registry=registry,
    )

    result = engine.run_messages(user_messages("重庆天气"))

    assert result.status == "success"  # 执行成功，只是工具返回结构化错误
    assert result.tool_results[0].error_code == "unavailable"
    assert result.answer == "抱歉，暂时查不到重庆的天气。"


def test_failed_dependency_blocks_only_dependent_steps():
    """一步失败不该让整个 Plan 崩掉：独立步骤照常执行，仍然给出回答。"""

    registry = make_registry(weather_failures={"重庆": "unavailable"})
    engine, _ = build_engine(
        [
            json_obj({}),
            json_obj(
                {
                    "need_info": [],
                    "steps": [
                        {
                            "id": 0,
                            "tool": "get_weather",
                            "arguments": {"city": "重庆"},
                            "depends_on": [],
                        },
                        {
                            "id": 1,
                            "tool": "get_calendar_events",
                            "arguments": {"date": "$steps.0.date"},
                            "depends_on": [0],
                        },
                        {
                            "id": 2,
                            "tool": "get_current_time",
                            "arguments": {},
                            "depends_on": [],
                        },
                    ],
                }
            ),
            text("重庆天气暂时查不到；当前时间已获取。"),
        ],
        registry=registry,
    )
    state = WorkingState(user_id="u1", session_id="s1")

    result = engine.run_messages(user_messages("重庆天气和现在几点"), working_state=state)

    assert result.status == "success"  # 不因为一步失败而整体失败
    assert result.tool_calls == ["get_weather", "get_current_time"]  # 被跳过的步骤不算调用

    by_step = {trace["id"]: trace for trace in result.step_traces}
    assert by_step[0]["error_code"] == "unavailable"
    assert by_step[1]["error_code"] == "blocked_by_dependency"
    assert by_step[2]["error_code"] is None
    assert result.answer


def test_fallback_summary_reports_blocked_steps():
    """汇总 LLM 也挂了时，回退文本仍要把失败原因告诉用户。"""

    registry = make_registry(weather_failures={"重庆": "unavailable"})
    engine, _ = build_engine(
        [
            json_obj({}),
            json_obj(
                {
                    "need_info": [],
                    "steps": [
                        {
                            "id": 0,
                            "tool": "get_weather",
                            "arguments": {"city": "重庆"},
                            "depends_on": [],
                        },
                        {
                            "id": 1,
                            "tool": "get_calendar_events",
                            "arguments": {"date": "$steps.0.date"},
                            "depends_on": [0],
                        },
                    ],
                }
            ),
            fail(TimeoutError("summary down")),
        ],
        registry=registry,
    )

    result = engine.run_messages(user_messages("重庆天气和日程"))

    assert "blocked_by_dependency" in result.answer
    assert "unavailable" in result.answer


def test_summary_failure_falls_back_to_observation_dump():
    engine, _ = build_engine(
        [
            json_obj({}),
            json_obj(
                {
                    "need_info": [],
                    "steps": [
                        {"id": 0, "tool": "get_current_time", "arguments": {}, "depends_on": []}
                    ],
                }
            ),
            fail(TimeoutError("summary down")),
        ]
    )

    result = engine.run_messages(user_messages("现在几点"))

    assert result.status == "success"
    assert "get_current_time" in result.answer  # 回退成观测结果清单
    assert "16:30" in result.answer


def test_invalid_arguments_in_plan_step_do_not_crash():
    engine, _ = build_engine(
        [
            json_obj({}),
            json_obj(
                {
                    "need_info": [],
                    "steps": [
                        # get_weather 缺少必填 city
                        {"id": 0, "tool": "get_weather", "arguments": {}, "depends_on": []}
                    ],
                }
            ),
            text("需要告诉我城市。"),
        ]
    )

    result = engine.run_messages(user_messages("查一下天气"))

    assert result.status == "success"
    assert result.tool_results[0].error_code == "invalid_arguments"


# -- 门面与评测接线 --------------------------------------------------------


def build_runtime(script, *, registry=None):
    sdk = ScriptedLLM(script)
    db = Database(":memory:")
    registry = registry or make_registry()
    client = LLMClient(sdk)
    runtime = ToolAgentRuntime(
        engine=ReactEngine(client, registry),
        plan_engine=PlanExecuteEngine(client, registry),
        sessions=SessionStore(db),
        memories=MemoryStore(db),
        resolver=SessionResolver(id_factory=lambda: "s_fixed"),
    )
    return runtime, sdk


def test_plan_mode_through_runtime_records_mode_and_answer():
    runtime, _ = build_runtime(
        [
            json_obj({"destination": "重庆"}),
            json_obj(
                {
                    "need_info": [],
                    "steps": [
                        {
                            "id": 0,
                            "tool": "get_weather",
                            "arguments": {"city": "重庆"},
                            "depends_on": [],
                        }
                    ],
                }
            ),
            text("重庆今天多云。"),
        ]
    )

    outcome = runtime.chat("重庆天气", user_id="u1", session_id="s1", mode="plan")

    assert outcome.mode == "plan"
    assert outcome.result.status == "success"
    assert outcome.result.answer == "重庆今天多云。"


def test_plan_mode_persists_pending_slots_through_runtime():
    runtime, _ = build_runtime(
        [
            json_obj({"purpose": "看电影"}),
            json_obj({"need_info": ["目的地"], "steps": [], "reason": "缺目的地"}),
        ]
    )

    outcome = runtime.chat("我想去看电影", user_id="u1", session_id="s1", mode="plan")

    assert outcome.result.status == "need_info"
    state = runtime.sessions.load_working_state(outcome.ref)
    assert state.pending_slots == ["destination"]  # 规范槽位名（跨轮持久化用）
    assert state.purpose == "看电影"


def test_unknown_mode_is_rejected():
    runtime, _ = build_runtime([])

    try:
        runtime.chat("你好", user_id="u1", session_id="s1", mode="graph")
    except ValueError as exc:
        assert "未知对话模式" in str(exc)
    else:  # pragma: no cover - 未抛错即失败
        raise AssertionError("未知模式应当报错")


def test_long_term_memory_reaches_the_planner():
    """design 8.2「合并 Long-term Memory 默认值」：记忆随 system 上下文进入规划器。"""

    runtime, sdk = build_runtime(
        [
            json_obj({"date": "明天"}),
            json_obj(
                {
                    "need_info": [],
                    "steps": [
                        {
                            "id": 0,
                            "tool": "get_weather",
                            "arguments": {"city": "重庆"},
                            "depends_on": [],
                        }
                    ],
                }
            ),
            text("明天重庆多云。"),
        ]
    )
    runtime.remember(
        MemoryCandidate(
            user_id="u1", key="home_city", value="重庆", category="user_identity"
        )
    )

    runtime.chat("明天出门合适吗", user_id="u1", session_id="s1", mode="plan")

    planner_input = sdk.calls[1]["messages"][-1]["content"]  # 第 2 次调用 = 规划器
    assert "home_city：重庆" in planner_input


def test_case_plan_012_passes_through_harness():
    """用例 plan_double_datetime_weather_012：``$steps`` 引用被真正解析。"""

    cases = {case.case_id: case for case in load_cases(CASES_PATH)}
    runtime, _ = build_runtime(
        [
            json_obj({"destination": "重庆"}),
            json_obj(
                {
                    "need_info": [],
                    "reason": "先解析日期再查天气",
                    "steps": [
                        {
                            "id": 0,
                            "tool": "resolve_datetime",
                            "arguments": {"expression": "明天"},
                            "depends_on": [],
                        },
                        {
                            "id": 1,
                            "tool": "get_weather",
                            "arguments": {"city": "重庆", "date": "$steps.0.date"},
                            "depends_on": [0],
                        },
                    ],
                }
            ),
            text("明天（2026-10-11）重庆多云。"),
        ]
    )

    check = run_case(cases["plan_double_datetime_weather_012"], runtime_engine(runtime))

    assert check.error is None
    assert check.outcome.tool_calls == ["resolve_datetime", "get_weather"]
    # 引用必须解析成实际日期，而不是原样透传 "$steps.0.date"
    assert check.outcome.arguments["get_weather"] == [{"city": "重庆", "date": "2026-10-11"}]
    assert check.passed is True
