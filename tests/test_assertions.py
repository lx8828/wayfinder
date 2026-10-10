"""断言设计：证据层（target）与容错匹配（any_of / regex）。

这一组测试要钉住的是一条**测评原则**：

> 业务上正确但措辞不同的回答，不能被判失败。

因此「关键实体 / 数值 / 日期」断言在 ``observation``（工具返回的确定数据）上，
「追问哪一项」断言在 ``need_info``（引擎结构化输出）上，「失败原因」断言在 ``tool_error`` 上，
只有**措辞稳定的确定性输出**（如工具直接给出的 ``16:30``）才断言在 ``final_answer`` 上，
自由文本一律交给 LLM-as-Judge。
"""

from __future__ import annotations

import pytest

from wayfinder.evals.case import (
    Assertion,
    EvalCase,
    evaluate_assertion,
    evidence_text,
    load_cases,
    validate_assertion,
)
from wayfinder.evals.runner import RunOutcome

# -- 证据层 ----------------------------------------------------------------


def outcome(**overrides) -> RunOutcome:
    base: dict = {
        "answer": "明天重庆多云，26 度左右。",
        "observations": [
            {"tool": "get_weather", "data": {"city": "重庆", "condition": "多云", "temperature_c": 26}}
        ],
        "need_info": ["destination"],
        "tool_errors": [{"tool": "get_weather", "error_code": "unavailable"}],
    }
    base.update(overrides)
    return RunOutcome(**base)


def test_observation_layer_reads_tool_data_not_the_answer():
    """关键事实断言在观测层：回答怎么措辞都不影响它。"""

    assertion = Assertion(type="contains", target="observation", value="多云")

    assert evaluate_assertion(assertion, outcome()) is True
    # 回答换了措辞（甚至完全没提），观测层的判定不变
    assert evaluate_assertion(assertion, outcome(answer="查到了。")) is True
    assert evaluate_assertion(assertion, outcome(observations=[])) is False


def test_observation_layer_flattens_nested_data():
    """嵌套结构（列表/数值/子对象）都要能被匹配到。"""

    nested = outcome(
        observations=[
            {
                "tool": "get_calendar_events",
                "data": {"date": "2026-10-11", "events": ["14:00 项目评审会"], "count": 1},
            }
        ]
    )

    for value in ("项目评审会", "2026-10-11", "14:00", "1"):
        assert evaluate_assertion(
            Assertion(type="contains", target="observation", value=value), nested
        ), value


def test_need_info_layer_uses_structured_slots():
    """追问断言在引擎的结构化输出上——**不要求中文回答里出现英文 slot 名**。"""

    assertion = Assertion(type="contains", target="need_info", value="destination")

    assert evaluate_assertion(assertion, outcome(answer="请问是哪个城市？")) is True
    assert evaluate_assertion(assertion, outcome(need_info=[])) is False


def test_tool_error_layer_uses_error_codes():
    assertion = Assertion(type="contains", target="tool_error", value="unavailable")

    assert evaluate_assertion(assertion, outcome(answer="抱歉，暂时查不到。")) is True
    assert evaluate_assertion(assertion, outcome(tool_errors=[])) is False


def test_final_answer_layer_still_works_for_deterministic_output():
    """只有确定性输出才断言在回答上（这里是工具直接给出的时间）。"""

    time_outcome = outcome(
        answer="现在是 16:30。",
        observations=[{"tool": "get_current_time", "data": {"time": "16:30"}}],
    )

    assert evaluate_assertion(Assertion(type="contains", value="16:30"), time_outcome) is True
    assert evaluate_assertion(Assertion(type="not_contains", value="晴"), outcome()) is True


def test_evidence_text_is_scoped_per_target():
    assert "多云" in evidence_text(Assertion(target="observation"), outcome())
    assert evidence_text(Assertion(target="need_info"), outcome()) == "destination"
    assert "unavailable" in evidence_text(Assertion(target="tool_error"), outcome())
    assert evidence_text(Assertion(target="final_answer"), outcome()) == outcome().answer


# -- 容错匹配 --------------------------------------------------------------


@pytest.mark.parametrize(
    "answer",
    [
        "重庆明天多云，26℃。",
        "根据预报，明天重庆的天气是多云。",
        "明天多云（重庆）。",
    ],
)
def test_any_of_accepts_synonymous_phrasings(answer):
    """同义表达必须通过——这正是旧实现「尺子太硬」的地方。"""

    assertion = Assertion(type="any_of", values=["重庆", "山城"])

    assert evaluate_assertion(assertion, outcome(answer=answer)) is True


def test_any_of_fails_when_none_of_the_candidates_appear():
    assertion = Assertion(type="any_of", values=["北京", "成都"])

    assert evaluate_assertion(assertion, outcome(answer="明天多云。")) is False


def test_regex_accepts_multiple_date_formats():
    assertion = Assertion(type="regex", value=r"2026-10-11|10\s*月\s*11")

    assert evaluate_assertion(assertion, outcome(answer="2026-10-11 有安排")) is True
    assert evaluate_assertion(assertion, outcome(answer="10 月 11 日有安排")) is True
    assert evaluate_assertion(assertion, outcome(answer="明天下午有安排")) is False


# -- 加载期校验 ------------------------------------------------------------


def test_validate_assertion_rejects_incomplete_specs():
    with pytest.raises(ValueError, match="any_of"):
        validate_assertion("c", Assertion(type="any_of", values=[]))
    with pytest.raises(ValueError, match="value"):
        validate_assertion("c", Assertion(type="contains", value=""))
    with pytest.raises(ValueError, match="regex"):
        validate_assertion("c", Assertion(type="regex", value="(["))


def test_load_cases_rejects_malformed_assertions(tmp_path):
    import json

    path = tmp_path / "cases.jsonl"
    path.write_text(
        json.dumps(
            {
                "case_id": "bad",
                "mode": "react",
                "input": "x",
                "assertions": [{"type": "any_of", "values": []}],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="any_of"):
        load_cases(path)


# -- 用例集的断言分布（防止又写回"写死措辞"） -----------------------------


def test_case_set_has_no_brittle_answer_only_fact_assertions():
    """事实类断言不该落在回答文本上。

    具体规则：``observation`` / ``need_info`` / ``tool_error`` 层的断言数量应显著多于
    ``final_answer`` 层的 ``contains``——后者每多一条，就多一把"措辞尺子"。
    这条测试是**防回退**用的：改回写死措辞会让它失败。
    """

    import json
    from pathlib import Path

    import wayfinder.evals as evals_pkg

    path = Path(evals_pkg.__file__).parent / "cases.jsonl"
    cases = load_cases(path)
    structured = 0
    answer_literals = 0
    for case in cases:
        for assertion in case.assertions:
            if assertion.target in ("observation", "need_info", "tool_error"):
                structured += 1
            elif assertion.target == "final_answer" and assertion.type in ("contains", "equals"):
                answer_literals += 1
    assert structured >= answer_literals * 2, (structured, answer_literals)
    # 确定性输出的字符串断言保留，但要少（现在只剩 1+1 这类）
    assert answer_literals <= 3


def test_eval_case_assertion_defaults_are_backwards_compatible():
    """未声明 target 的旧写法默认落在 final_answer 上。"""

    case = EvalCase(case_id="c", mode="react", input="x", assertions=[{"type": "contains", "value": "好"}])

    assertion = case.assertions[0]
    assert assertion.target == "final_answer"
    assert evaluate_assertion(assertion, outcome(answer="好的")) is True
