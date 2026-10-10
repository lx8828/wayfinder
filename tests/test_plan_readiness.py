"""规划前置校验与复合意图覆盖度（R26 / R27）。

要钉住的行为：

1. **缺槽位时不许硬跑**：计划里出现「不在已知信息中」的地点，或「我家/公司」这类相对说法，
   都必须先追问缺的槽位，**不执行任何工具**。
2. **有默认值的参数不算缺槽位**：模型可以用默认值（如 `mode=car`），不能因此拦住计划。
3. **复合意图漏步骤**：只触发一次修复性重规划；重规划仍不达标也照常执行（启发式不否决计划）。
4. **空计划**（既无步骤也无 need_info）是结构缺陷，不是"成功"。
"""

from __future__ import annotations

from first_agent.evals.scripted import json_obj, text
from tests.plan_helpers import build_engine, user_messages

INVENTED_CITY_PLAN = {
    "need_info": [],
    "reason": "用户没说城市，规划器自己编了一个",
    "steps": [{"id": 0, "tool": "get_weather", "arguments": {"city": "北京"}, "depends_on": []}],
}

RELATIVE_PLACE_PLAN = {
    "need_info": [],
    "reason": "直接用「我家」当出发地",
    "steps": [
        {
            "id": 0,
            "tool": "estimate_travel_time",
            "arguments": {"origin": "我家", "destination": "成都", "mode": "car"},
            "depends_on": [],
        }
    ],
}


# -- 1. 缺槽位 → 追问，不执行 ------------------------------------------------


def test_plan_with_invented_city_asks_instead_of_executing():
    engine, sdk = build_engine([json_obj({}), json_obj(INVENTED_CITY_PLAN)])

    result = engine.run_messages(user_messages("明天天气怎么样？"))

    assert result.status == "need_info"
    assert result.tool_calls == []  # 关键：一个工具都没执行
    assert result.need_info == ["destination"]
    assert "目的地" in result.answer  # 面向用户说中文
    assert result.plan_valid is False  # 计划本身有缺陷（被拦下）
    assert len(sdk.calls) == 2  # 槽位 + 计划：不重规划（缺的信息重规划也变不出来）


def test_relative_place_is_treated_as_missing_slot():
    engine, sdk = build_engine([json_obj({}), json_obj(RELATIVE_PLACE_PLAN)])

    result = engine.run_messages(user_messages("从我家出发去成都，路上要多久？"))

    assert result.status == "need_info"
    assert result.tool_calls == []
    assert result.need_info == ["origin"]  # 目的地成都已给出，缺的是出发地
    assert "出发地" in result.answer
    assert len(sdk.calls) == 2


def test_value_from_memory_is_not_treated_as_invented():
    """「已知用户信息」里出现过的值不算编造（否则会把正确的计划拦下来）。"""

    plan = {
        "need_info": [],
        "steps": [{"id": 0, "tool": "get_weather", "arguments": {"city": "重庆"}, "depends_on": []}],
    }
    engine, _ = build_engine(
        [json_obj({"destination": "重庆"}), json_obj(plan), text("重庆多云。")]
    )

    # 输入里没有城市，但槽位（来自记忆/追问）里有
    result = engine.run_messages(user_messages("明天天气怎么样？"))

    assert result.status == "success"
    assert result.tool_calls == ["get_weather"]


def test_derivable_date_is_not_treated_as_invented():
    """日期是可推导的（resolve_datetime / 日期运算），不能因为"输入里没有这个日期字符串"就拦下。

    真跑中这条规则误拦过正确的计划（模型自己算出 2026-10-11 直接查日历）。
    """

    plan = {
        "need_info": [],
        "steps": [
            {
                "id": 0,
                "tool": "get_calendar_events",
                "arguments": {"date": "2026-10-11"},
                "depends_on": [],
            }
        ],
    }
    engine, _ = build_engine([json_obj({}), json_obj(plan), text("明天下午有评审会。")])

    result = engine.run_messages(user_messages("我明天下午有什么安排？"))

    assert result.status == "success"
    assert result.tool_calls == ["get_calendar_events"]


def test_need_info_synonyms_are_normalized_to_canonical_slots():
    """need_info 是结构化契约：模型写 ``city`` 也要落到规范槽位 ``destination``。"""

    plan = {"need_info": ["city"], "steps": [], "reason": "不知道城市"}
    engine, _ = build_engine([json_obj({}), json_obj(plan)])

    result = engine.run_messages(user_messages("明天天气怎么样？"))

    assert result.status == "need_info"
    assert result.need_info == ["destination"]
    assert "目的地" in result.answer


def test_optional_arguments_are_not_slot_checked():
    """有默认值的参数允许模型用默认值：transport_mode 槽位为空不能拦住计划。"""

    plan = {
        "need_info": [],
        "steps": [
            {
                "id": 0,
                "tool": "estimate_travel_time",
                "arguments": {"origin": "重庆", "destination": "成都", "mode": "car"},
                "depends_on": [],
            }
        ],
    }
    engine, _ = build_engine([json_obj({}), json_obj(plan), text("约 320 分钟。")])

    result = engine.run_messages(user_messages("从重庆到成都开车要多久？"))

    assert result.status == "success"
    assert result.tool_calls == ["estimate_travel_time"]


# -- 2. 复合意图覆盖度 ------------------------------------------------------


def test_composite_intent_triggers_one_repair_replan():
    """「天气和日程允许吗」只计划了一步 → 触发一次重规划，补齐另一个需求。"""

    partial = {
        "need_info": [],
        "steps": [
            {
                "id": 0,
                "tool": "resolve_datetime",
                "arguments": {"expression": "明天下午"},
                "depends_on": [],
            }
        ],
    }
    complete = {
        "need_info": [],
        "steps": [
            {
                "id": 0,
                "tool": "resolve_datetime",
                "arguments": {"expression": "明天下午"},
                "depends_on": [],
            },
            {
                "id": 1,
                "tool": "get_weather",
                "arguments": {"city": "重庆", "date": "$steps.0.date"},
                "depends_on": [0],
            },
            {
                "id": 2,
                "tool": "get_calendar_events",
                "arguments": {"date": "$steps.0.date"},
                "depends_on": [0],
            },
        ],
    }
    engine, sdk = build_engine(
        [
            json_obj({"destination": "重庆"}),
            json_obj(partial),
            json_obj(complete),
            text("天气不错，但下午有安排。"),
        ]
    )

    result = engine.run_messages(user_messages("我明天下午想去公园，天气和日程允许吗？"))

    assert result.tool_calls == ["resolve_datetime", "get_weather", "get_calendar_events"]
    assert result.status == "success"
    assert len(sdk.calls) == 4  # 槽位 + 计划 + 一次修复性重规划 + 汇总


def test_coverage_gap_never_blocks_a_structurally_valid_plan():
    """覆盖度只是启发式：重规划仍不达标也**照常执行**，不能因此降级。"""

    one_step = {
        "need_info": [],
        "steps": [
            {"id": 0, "tool": "get_current_time", "arguments": {}, "depends_on": []}
        ],
    }
    engine, _ = build_engine(
        [json_obj({}), json_obj(one_step), json_obj(one_step), text("现在是 16:30。")]
    )

    result = engine.run_messages(user_messages("现在几点和重庆天气？"))

    assert result.tool_calls == ["get_current_time"]
    assert result.status == "success"
    assert result.degraded_to is None  # 启发式没有把计划判死


# -- 3. 空计划是结构缺陷 ----------------------------------------------------


def test_empty_plan_is_repaired_by_replan():
    """真实运行出现过「零工具调用且不追问」的空计划 → 静默的空洞回答。"""

    empty = {"need_info": [], "steps": [], "reason": "什么都没规划"}
    good = {
        "need_info": [],
        "steps": [{"id": 0, "tool": "get_current_time", "arguments": {}, "depends_on": []}],
    }
    engine, _ = build_engine([json_obj({}), json_obj(empty), json_obj(good), text("现在是 16:30。")])

    result = engine.run_messages(user_messages("现在几点？"))

    assert result.status == "success"
    assert result.tool_calls == ["get_current_time"]


def test_empty_plan_after_replan_does_not_claim_success():
    """重规划仍为空 → 必须走失败路径，而不是拿零观测去汇总。"""

    empty = {"need_info": [], "steps": [], "reason": "还是空的"}
    engine, _ = build_engine([json_obj({}), json_obj(empty), json_obj(empty)])

    result = engine.run_messages(user_messages("现在几点？"))

    assert result.status in {"invalid_plan", "error"}
    assert result.plan_valid is False
    assert result.tool_calls == []
