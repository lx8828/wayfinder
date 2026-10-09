"""评测护栏测试：用例加载、判定、指标、回归门禁与端到端离线跑通。

`test_end_to_end_*` 用 ScriptedLLM + Fake provider 组装的**测试内引擎**验证护栏接线；
真实 ReActEngine 在 S3 接入。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

import first_agent.evals as evals_pkg
from first_agent.core.client import LLMClient
from first_agent.evals import (
    EvalCase,
    RunOutcome,
    ScriptedLLM,
    compare_metrics,
    compute_metrics,
    effective_max_steps,
    format_diff,
    gate_failures,
    load_cases,
    run_case,
    run_cases,
    text,
    tool,
)
from first_agent.evals.case import (
    arguments_match,
    memory_delta_matches,
    sequence_matches,
    set_matches,
)
from first_agent.providers import FakeWeatherProvider
from first_agent.schemas.tool import ToolSpec
from first_agent.tools.registry import ToolRegistry

CASES_PATH = Path(evals_pkg.__file__).parent / "cases.jsonl"


# -- 加载 ------------------------------------------------------------------


def test_load_seed_cases():
    cases = load_cases(CASES_PATH)
    assert len(cases) == 5
    assert {case.case_id for case in cases} == {
        "react_001",
        "react_002",
        "react_003",
        "plan_001",
        "memory_001",
    }


def test_load_cases_rejects_duplicate_ids(tmp_path):
    path = tmp_path / "cases.jsonl"
    line = json.dumps({"case_id": "x", "mode": "react", "input": "hi"})
    path.write_text(f"{line}\n{line}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="重复"):
        load_cases(path)


def test_load_cases_reports_line_number_on_parse_error(tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text(
        '{"case_id": "x", "mode": "react", "input": "hi"}\n不是 json\n', encoding="utf-8"
    )

    with pytest.raises(ValueError, match=":2"):
        load_cases(path)


def test_effective_max_steps_respects_hard_cap():
    assert effective_max_steps(EvalCase(case_id="a", mode="react", input="x")) == 5
    assert effective_max_steps(EvalCase(case_id="a", mode="react", input="x", max_steps=3)) == 3
    assert effective_max_steps(EvalCase(case_id="a", mode="react", input="x", max_steps=99)) == 8


# -- 判定 ------------------------------------------------------------------


def test_set_matches_distinguishes_unset_from_empty():
    unset = EvalCase(case_id="a", mode="react", input="x")
    assert set_matches(unset, ["get_weather"]) is None  # 未声明 → 不约束

    empty = EvalCase(case_id="a", mode="react", input="x", expected_tool_set=[])
    assert set_matches(empty, []) is True
    assert set_matches(empty, ["get_weather"]) is False


def test_sequence_matches_prefers_allowed_topological_orders():
    case = EvalCase(
        case_id="a",
        mode="plan",
        input="x",
        expected_tool_sequence=["a", "b"],
        allowed_topological_orders=[["b", "a"]],
    )
    assert sequence_matches(case, ["b", "a"]) is True
    assert sequence_matches(case, ["a", "b"]) is False


def test_sequence_matches_ignored_for_react():
    case = EvalCase(case_id="a", mode="react", input="x", expected_tool_sequence=["a"])
    assert sequence_matches(case, ["a"]) is None


def test_arguments_match_uses_subset_semantics():
    case = EvalCase(
        case_id="a",
        mode="react",
        input="x",
        expected_arguments={"get_weather": {"city": "重庆"}},
    )
    assert arguments_match(case, {"get_weather": [{"city": "重庆", "date": "d"}]}) is True
    assert arguments_match(case, {"get_weather": [{"city": "北京"}]}) is False
    assert arguments_match(case, {}) is False


def test_memory_delta_empty_means_no_writes():
    case = EvalCase(case_id="a", mode="react", input="x", expected_memory_delta={})
    assert memory_delta_matches(case, {}) is True
    assert memory_delta_matches(case, {"home_city": "add"}) is False


# -- 指标 ------------------------------------------------------------------


def test_compute_metrics_basic():
    cases = [
        EvalCase(
            case_id="a",
            mode="react",
            input="x",
            expected_tool_set=["get_weather"],
            expected_arguments={"get_weather": {"city": "重庆"}},
        ),
        EvalCase(case_id="b", mode="react", input="x", expected_tool_set=["get_weather"]),
    ]
    checks = [
        run_case(
            cases[0],
            lambda _case: RunOutcome(
                answer="ok",
                tool_calls=["get_weather"],
                arguments={"get_weather": [{"city": "重庆"}]},
                steps=2,
                latency_ms=10,
            ),
        ),
        run_case(cases[1], lambda _case: RunOutcome(answer="ok", steps=1, latency_ms=20)),
    ]

    metrics = compute_metrics(checks)
    assert metrics["Tool Selection Accuracy"] == 50.0
    assert metrics["Argument Accuracy"] == 100.0
    assert metrics["Average Steps"] == 1.5
    assert metrics["Average Latency"] == 15.0


def test_extra_tool_call_rate_counts_repeats():
    case = EvalCase(case_id="a", mode="react", input="x", expected_tool_set=["get_weather"])
    check = run_case(
        case, lambda _case: RunOutcome(tool_calls=["get_weather", "get_weather"], steps=2)
    )

    assert check.extra_calls == 1
    assert compute_metrics([check])["Extra Tool Call Rate"] == 50.0


def test_compute_metrics_omits_unspecified_dimensions():
    case = EvalCase(case_id="a", mode="react", input="x")
    metrics = compute_metrics([run_case(case, lambda _case: RunOutcome(answer="x", steps=1))])

    assert "Tool Selection Accuracy" not in metrics
    assert "Argument Accuracy" not in metrics
    assert "Extra Tool Call Rate" not in metrics  # 没有工具调用


# -- 回归门禁 --------------------------------------------------------------


def test_compare_metrics_is_direction_aware():
    baseline = {
        "Tool Selection Accuracy": 90.0,
        "Extra Tool Call Rate": 10.0,
        "Average Latency": 100.0,
    }
    current = {
        "Tool Selection Accuracy": 88.5,
        "Extra Tool Call Rate": 9.0,
        "Average Latency": 130.0,
    }
    deltas = {delta.name: delta for delta in compare_metrics(baseline, current)}

    assert deltas["Tool Selection Accuracy"].worsened  # 越高越好 → 下降即恶化
    assert deltas["Tool Selection Accuracy"].gate_failed  # 1.5pt > 1pt 容差
    assert not deltas["Extra Tool Call Rate"].worsened  # 越低越好 → 下降是变好
    assert deltas["Average Latency"].worsened  # 越低越好 → 上升即恶化
    assert not deltas["Average Latency"].gate_failed  # 波动指标不进硬门禁
    assert [delta.name for delta in gate_failures(list(deltas.values()))] == [
        "Tool Selection Accuracy"
    ]


def test_compare_metrics_within_tolerance_is_not_gate_failure():
    deltas = compare_metrics({"Tool Selection Accuracy": 90.0}, {"Tool Selection Accuracy": 89.5})
    assert deltas[0].worsened
    assert not deltas[0].gate_failed


def test_compare_metrics_handles_missing_side():
    deltas = compare_metrics({"Tool Selection Accuracy": 90.0}, {})
    assert deltas[0].delta is None
    assert not deltas[0].worsened


def test_format_diff_marks_gate_failure():
    rendered = format_diff(
        compare_metrics({"Tool Selection Accuracy": 90.0}, {"Tool Selection Accuracy": 80.0})
    )
    assert "门禁失败" in rendered


# -- 端到端：无 API Key 跑通假用例 -----------------------------------------


class WeatherIn(BaseModel):
    city: str
    date: str | None = None


class WeatherOut(BaseModel):
    city: str
    date: str
    condition: str
    temperature_c: float
    feels_like_c: float
    humidity: int
    source: str


def make_stub_engine(script, provider) -> object:
    """测试内最小 ReAct 引擎，仅用于验证护栏接线（真实引擎见 S3）。"""

    client = LLMClient(ScriptedLLM(script))
    registry = ToolRegistry()
    registry.register(
        ToolSpec(
            name="get_weather",
            description="查询天气",
            input_model=WeatherIn,
            output_model=WeatherOut,
            handler=lambda payload: provider.get_weather(payload.city, payload.date or "2026-10-10"),
        )
    )

    def engine(case: EvalCase) -> RunOutcome:
        messages = [
            {"role": "system", "content": "你是出行助手"},
            {"role": "user", "content": case.input},
        ]
        tool_names: list[str] = []
        arguments: dict[str, list[dict]] = {}
        steps = 0
        for _ in range(effective_max_steps(case)):
            steps += 1
            turn = client.chat_turn(messages, tools=registry.schemas())
            if not turn.tool_calls:
                return RunOutcome(
                    answer=turn.thought or "",
                    tool_calls=tool_names,
                    arguments=arguments,
                    steps=steps,
                )
            messages.append(
                {
                    "role": "assistant",
                    "content": turn.thought,
                    "tool_calls": [
                        {
                            "id": call.call_id,
                            "type": "function",
                            "function": {
                                "name": call.tool_name,
                                "arguments": json.dumps(call.arguments, ensure_ascii=False),
                            },
                        }
                        for call in turn.tool_calls
                    ],
                }
            )
            for call in turn.tool_calls:
                spec = registry.get(call.tool_name)
                payload = spec.input_model.model_validate(call.arguments)
                data = spec.output_model.model_validate(spec.handler(payload)).model_dump()
                tool_names.append(call.tool_name)
                arguments.setdefault(call.tool_name, []).append(call.arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": json.dumps(data, ensure_ascii=False),
                    }
                )
        return RunOutcome(
            tool_calls=tool_names, arguments=arguments, steps=steps, status="budget_exceeded"
        )

    return engine


def test_end_to_end_case_passes_without_api_key():
    cases = {case.case_id: case for case in load_cases(CASES_PATH)}
    engine = make_stub_engine(
        [tool("get_weather", {"city": "北京"}, thought="先查天气"), text("北京今天晴，28°C")],
        FakeWeatherProvider(),
    )

    check = run_case(cases["react_001"], engine)

    assert check.error is None
    assert check.outcome.tool_calls == ["get_weather"]
    assert check.passed is True


def test_end_to_end_memory_case_uses_initial_memory():
    cases = {case.case_id: case for case in load_cases(CASES_PATH)}
    case = cases["react_002"]
    provider = FakeWeatherProvider()
    engine = make_stub_engine(
        [
            tool("get_weather", {"city": case.initial_memory["home_city"]}),
            text("重庆今天多云"),
        ],
        provider,
    )

    check = run_case(case, engine)

    assert check.passed is True
    assert provider.calls == [("重庆", "2026-10-10")]


def test_end_to_end_react_case_without_tools_expected():
    cases = {case.case_id: case for case in load_cases(CASES_PATH)}
    engine = make_stub_engine([text("你好！有什么可以帮你的？")], FakeWeatherProvider())

    check = run_case(cases["react_003"], engine)

    assert check.outcome.tool_calls == []
    assert check.passed is True


def test_run_case_turns_engine_crash_into_failure():
    def crash(_case: EvalCase) -> RunOutcome:
        raise RuntimeError("engine down")

    case = EvalCase(case_id="boom", mode="react", input="x", expected_status="success")
    checks = run_cases([case], crash)

    assert checks[0].passed is False
    assert checks[0].outcome.status == "error"
    assert "engine down" in checks[0].outcome.error
