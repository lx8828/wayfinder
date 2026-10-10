"""S7：评测编排、baseline 门禁与离线夹具的测试（design.md 16.4 / 16.5 / 16.6）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import wayfinder.evals as evals_pkg
from wayfinder.evals import judge as judge_mod
from wayfinder.evals import offline, suite
from wayfinder.evals.case import EvalCase, OfflineFixture, load_cases
from wayfinder.evals.metrics import MetricDelta, compare_metrics
from wayfinder.evals.runner import RunOutcome, run_case, run_cases_isolated

CASES_PATH = Path(evals_pkg.__file__).parent / "cases.jsonl"


def _case(case_id: str) -> EvalCase:
    return {case.case_id: case for case in load_cases(CASES_PATH)}[case_id]


# -- 门禁 ------------------------------------------------------------------


def test_gate_passes_when_there_is_no_baseline():
    result = suite.evaluate_gate(None, {"Tool Selection Accuracy": 100.0})

    assert result.passed is True
    assert result.failed_deltas == []


def test_gate_fails_on_stable_degradation_beyond_tolerance():
    baseline = {"Tool Selection Accuracy": 100.0}
    current = {"Tool Selection Accuracy": 97.0}

    result = suite.evaluate_gate(baseline, current)

    assert result.passed is False
    assert [delta.name for delta in result.failed_deltas] == ["Tool Selection Accuracy"]


def test_gate_does_not_fail_within_tolerance():
    result = suite.evaluate_gate(
        {"Tool Selection Accuracy": 100.0}, {"Tool Selection Accuracy": 99.5}
    )

    assert result.passed is True
    assert result.warnings == []


def test_volatile_metric_only_warns_when_beyond_tolerance():
    baseline = {"Average Latency": 1000.0}

    small = suite.evaluate_gate(baseline, {"Average Latency": 1000.2})
    large = suite.evaluate_gate(baseline, {"Average Latency": 1100.0})

    # 0.2 的抖动不该告警（否则会出现"告警疲劳"）
    assert small.passed is True and small.warnings == []
    assert large.passed is True
    assert [delta.name for delta in large.warnings] == ["Average Latency"]


def test_direction_awareness_for_lower_is_better_metrics():
    deltas = compare_metrics({"Average Steps": 1.5}, {"Average Steps": 2.5})

    (delta,) = deltas
    assert delta.higher_is_better is False
    assert delta.degradation == pytest.approx(1.0)


# -- 元数据与产物 ----------------------------------------------------------


def test_metadata_records_versions():
    case = _case("react_single_weather_001")
    metadata = suite.build_metadata(
        cases_path=CASES_PATH,
        mode="offline",
        model="scripted+fake",
        registry=offline.build_registry(case),
    )

    assert metadata["case_count"] == 0  # 由 run_suite 补上
    assert len(metadata["case_set_hash"]) == 12
    assert len(metadata["prompt_version"]) == 12
    assert len(metadata["tool_schema_version"]) == 12
    assert metadata["git_commit"]


def test_case_set_hash_depends_on_content(tmp_path):
    first = tmp_path / "a.jsonl"
    second = tmp_path / "b.jsonl"
    first.write_text('{"case_id": "x", "mode": "react", "input": "1"}\n', encoding="utf-8")
    second.write_text('{"case_id": "x", "mode": "react", "input": "2"}\n', encoding="utf-8")

    assert suite.case_set_hash(first) != suite.case_set_hash(second)
    assert suite.case_set_hash(first) == suite.case_set_hash(first)


def test_write_outputs_creates_current_and_diff(tmp_path):
    case = _case("react_single_weather_001")
    run = suite.run_suite(
        [case],
        offline.offline_engine_factory,
        metadata={"mode": "offline", "model": "scripted+fake"},
    )
    gate = suite.evaluate_gate(None, run.metrics)

    paths = suite.write_outputs(run, out_dir=tmp_path, gate=gate)

    payload = json.loads(Path(paths["current"]).read_text(encoding="utf-8"))
    assert payload["metrics"] == run.metrics
    diff = Path(paths["diff"]).read_text(encoding="utf-8")
    assert "门禁" in diff


def test_baseline_roundtrip(tmp_path):
    case = _case("react_single_weather_001")
    run = suite.run_suite(
        [case], offline.offline_engine_factory, metadata={"mode": "offline", "model": "x"}
    )
    path = tmp_path / "baseline.json"

    suite.save_baseline(path, run)

    loaded = suite.load_baseline(path)
    assert loaded == run.metrics
    assert suite.load_baseline(tmp_path / "missing.json") is None


def test_run_suite_fills_case_count():
    case = _case("react_single_weather_001")
    run = suite.run_suite(
        [case], offline.offline_engine_factory, metadata={"mode": "offline", "model": "x"}
    )

    assert run.metadata["case_count"] == 1
    assert run.failures == []


# -- 离线夹具 --------------------------------------------------------------


def test_offline_script_is_deterministic():
    case = _case("plan_double_datetime_weather_012")

    first = offline.build_script(case)
    second = offline.build_script(case)

    assert [type(step).__name__ for step in first] == [type(step).__name__ for step in second]
    assert len(first) == len(second)


def test_offline_plan_case_requires_an_explicit_plan():
    case = EvalCase(case_id="no_plan", mode="plan", input="明天天气")

    with pytest.raises(ValueError, match="offline.plan"):
        offline.build_script(case)


def test_offline_react_script_is_derived_from_expectations():
    case = _case("react_single_weather_001")

    steps = offline.build_script(case)

    # 推导：工具调用 + 最终回答 + 记忆抽取
    assert [type(step).__name__ for step in steps] == ["ToolStep", "TextStep", "JsonStep"]


def test_each_case_gets_its_own_database():
    """database 隔离（design.md 16.4）：两个运行时之间不共享任何记忆。"""

    from wayfinder.schemas.memory import MemoryCandidate

    case = _case("memory_add_028")
    first = offline.build_runtime(case)
    second = offline.build_runtime(case)
    user_id = "case:memory_add_028"

    first.memories.apply_candidate(
        MemoryCandidate(
            user_id=user_id, key="home_city", value="重庆", category="user_identity"
        )
    )

    assert first.memories.values(user_id) == {"home_city": "重庆"}
    assert second.memories.values(user_id) == {}


def test_run_cases_isolated_builds_one_engine_per_case():
    calls: list[str] = []

    def factory(case: EvalCase) -> object:
        calls.append(case.case_id)
        return lambda inner: RunOutcome(answer="ok")

    checks = run_cases_isolated(
        [_case("react_single_weather_001"), _case("no_tool_math_036")], factory
    )

    assert calls == ["react_single_weather_001", "no_tool_math_036"]
    assert len(checks) == 2


# -- 工具成功性护栏 --------------------------------------------------------


def test_tool_error_fails_the_case_even_when_expectations_match():
    """工具全挂但集合/参数恰好正确时，指标不能假装正常。"""

    case = EvalCase(
        case_id="x", mode="react", input="hi", expected_tool_set=["get_weather"]
    )

    def engine(_case: EvalCase) -> RunOutcome:
        return RunOutcome(
            tool_calls=["get_weather"],
            arguments={"get_weather": [{"city": "北京"}]},
            steps=1,
            tool_errors=[{"tool": "get_weather", "error_code": "invalid_output"}],
        )

    check = run_case(case, engine)

    assert check.checks["tool_ok"] is False
    assert check.passed is False


def test_expect_tool_errors_suppresses_the_guard():
    case = EvalCase(
        case_id="x",
        mode="react",
        input="hi",
        expected_tool_set=["get_weather"],
        expect_tool_errors=True,
    )

    def engine(_case: EvalCase) -> RunOutcome:
        return RunOutcome(
            tool_calls=["get_weather"],
            tool_errors=[{"tool": "get_weather", "error_code": "unavailable"}],
        )

    check = run_case(case, engine)

    assert check.checks["tool_ok"] is None
    assert check.passed is True


def test_failure_report_names_the_reason():
    case = EvalCase(
        case_id="bad_case", mode="react", input="hi", expected_tool_set=["get_weather"]
    )

    def engine(_case: EvalCase) -> RunOutcome:
        return RunOutcome(tool_calls=[])

    report = suite.failure_report([run_case(case, engine)])

    assert "bad_case" in report
    assert "tool_set=✗" in report


def test_failure_report_names_the_failing_assertion():
    """失败报告必须指出**是哪一条**断言失败——否则无法区分「模型错了」与「尺子太硬」。"""

    case = EvalCase(
        case_id="assert_case",
        mode="react",
        input="hi",
        expected_tool_set=["get_weather"],
        assertions=[
            {"type": "contains", "target": "observation", "value": "项目评审会"},
            {"type": "any_of", "values": ["评审", "会议"]},
        ],
    )

    def engine(_case: EvalCase) -> RunOutcome:
        return RunOutcome(
            answer="今天没有安排。",
            tool_calls=["get_weather"],
            observations=[{"tool": "get_weather", "data": {"city": "北京"}}],
        )

    report = suite.failure_report([run_case(case, engine)])

    assert "observation:contains(项目评审会)" in report
    assert "final_answer∈[评审/会议]" in report


def test_metric_delta_direction_is_declared_for_every_metric():
    deltas = compare_metrics(
        {"Extra Tool Call Rate": 0.0, "Average Steps": 1.0}, {"Extra Tool Call Rate": 5.0, "Average Steps": 2.0}
    )

    by_name = {delta.name: delta for delta in deltas}
    assert by_name["Extra Tool Call Rate"].higher_is_better is False
    assert by_name["Extra Tool Call Rate"].degradation == pytest.approx(5.0)
    assert isinstance(by_name["Average Steps"], MetricDelta)


# -- LLM-as-Judge（开放式用例） -------------------------------------------


JUDGE_CASES_PATH = Path(evals_pkg.__file__).parent / "judge_cases.jsonl"


def test_repeat_runs_average_metrics_and_report_consistency(tmp_path):
    case = _case("react_single_weather_001")

    repeats = suite.run_repeated(
        [case], offline.offline_engine_factory, metadata={"mode": "offline"}, repeat=2
    )

    assert repeats.repeat == 2
    assert repeats.metrics_mean["Tool Selection Accuracy"] == 100.0
    assert repeats.metrics_spread()["Tool Selection Accuracy"] == 0.0
    consistency = repeats.consistency()
    assert consistency["cases"] == 1
    assert consistency["passed_consistency"] == 100.0
    assert consistency["flaky_cases"] == []

    payload = json.loads(
        suite.write_repeats(tmp_path / "repeats.json", repeats).read_text(encoding="utf-8")
    )
    assert payload["repeat"] == 2
    assert len(payload["per_run_metrics"]) == 2
    assert "跨轮一致率" in suite.format_repeat_report(repeats)


def test_consistency_detects_flaky_cases():
    """跨轮结论不一致的用例必须被点名——否则「稳定性」无从谈起。"""

    case = _case("react_single_weather_001")
    answers = iter([True, False])

    def factory(_case: EvalCase):
        def engine(_inner: EvalCase) -> RunOutcome:
            ok = next(answers)
            return RunOutcome(
                answer="离线夹具回答：北京" if ok else "抱歉",
                tool_calls=["get_weather"] if ok else [],
                arguments={"get_weather": [{"city": "北京"}]} if ok else {},
                # 断言里有关键事实落在观测层：正常分支必须真的带回数据
                observations=(
                    [{"tool": "get_weather", "data": {"city": "北京", "condition": "晴"}}]
                    if ok
                    else []
                ),
                steps=1,
            )

        return engine

    repeats = suite.run_repeated([case], factory, metadata={"mode": "fake"}, repeat=2)
    consistency = repeats.consistency()

    assert consistency["passed_consistency"] == 0.0
    assert consistency["tool_sequence_consistency"] == 0.0
    assert consistency["flaky_cases"] == ["react_single_weather_001"]
    assert [check.case.case_id for check in repeats.all_failing_checks()] == [
        "react_single_weather_001"
    ]


def test_run_repeated_rejects_zero_repeats():
    with pytest.raises(ValueError, match="repeat"):
        suite.run_repeated([], offline.offline_engine_factory, metadata={}, repeat=0)


def test_judge_cases_are_loaded_and_have_criteria():
    cases = judge_mod.load_judge_cases(JUDGE_CASES_PATH)

    assert len(cases) == 15
    assert all(case.criteria.strip() for case in cases)
    assert len({case.case_id for case in cases}) == 15


def test_judge_case_set_is_large_enough_for_stable_rates():
    """单条用例的权重 = 100/N 个百分点（S15）。

    judge 只有 5 条时，一条用例翻转就是 **20pt** 的差距，读起来像"能力剧变"，
    实际只是裁判抖动。样本量必须够大，让单条用例的权重落到可忽略的量级。
    """

    cases = judge_mod.load_judge_cases(JUDGE_CASES_PATH)
    per_case = 100 / len(cases)

    assert len(cases) >= 15, f"judge 样本过少：{len(cases)} 条"
    assert per_case <= 7, f"单条用例权重 {per_case:.1f}pt 过大"


def test_judge_cases_cover_both_modes_and_key_scenarios():
    """覆盖面也要钉住：只堆同类用例，会得到"很稳但不说明问题"的指标。"""

    cases = judge_mod.load_judge_cases(JUDGE_CASES_PATH)
    modes = {case.mode for case in cases}
    asserted = " ".join(case.criteria for case in cases)

    assert modes == {"react", "plan"}
    # 记忆（有已知居住地 / 记忆被本轮覆盖）
    assert sum("居住地" in case.criteria for case in cases) >= 2
    # 失败诚实性与能力边界
    assert "不可用" in asserted and "订票" in asserted
    # 多城市对比
    assert any("重庆" in case.criteria and "成都" in case.criteria for case in cases)


def test_readme_numbers_match_the_real_case_files():
    """README 的可核对数字不能过期。

    S9 的教训：文档声称「三层记忆」但摘要生成其实没接线。用例数量是能被机器核对的那部分，
    所以由测试挡住「改了用例忘了改 README」。
    """

    readme = (JUDGE_CASES_PATH.parents[3] / "README.md").read_text(encoding="utf-8")
    case_count = len(load_cases(CASES_PATH))
    judge_count = len(judge_mod.load_judge_cases(JUDGE_CASES_PATH))

    assert f"{case_count} 条用例" in readme
    assert f"{judge_count} 条开放式用例" in readme


def test_judge_summary_shows_case_weight_and_spread():
    """摘要必须给出单条用例权重与轮间摆动，否则单轮会被读成结论。"""

    line = judge_mod.judge_summary([93.33, 73.33, 80.0], 15)

    assert "82.22" in line  # 均值
    assert "6.67pt" in line  # 单条用例权重 = 100/15
    assert "20.0pt" in line  # 轮间摆动
    assert "读均值" in line


def test_judge_summary_handles_no_valid_samples():
    assert "无有效样本" in judge_mod.judge_summary([None, None], 15)
    # 单轮时不提摆动（没有可比的轮次）
    assert "摆动" not in judge_mod.judge_summary([80.0], 15)


def test_judge_cases_reject_duplicate_ids(tmp_path):
    path = tmp_path / "judge.jsonl"
    line = json.dumps({"case_id": "dup", "input": "x", "criteria": "c"})
    path.write_text(f"{line}\n{line}\n", encoding="utf-8")

    with pytest.raises(ValueError, match="重复 case_id"):
        judge_mod.load_judge_cases(path)


def test_task_success_rate_excludes_unjudged_samples():
    """裁判失败的样本不计入分母——否则指标混入了裁判自身的波动。"""

    cases = [judge_mod.JudgeCase(case_id=f"j{i}", input="x", criteria="c") for i in range(3)]
    outcomes = [
        judge_mod.JudgeOutcome(
            case=cases[0], outcome=RunOutcome(answer="a"), verdict=judge_mod.JudgeVerdict(passed=True)
        ),
        judge_mod.JudgeOutcome(
            case=cases[1],
            outcome=RunOutcome(answer="b"),
            verdict=judge_mod.JudgeVerdict(passed=False),
        ),
        judge_mod.JudgeOutcome(case=cases[2], outcome=RunOutcome(answer="c"), verdict=None),
    ]

    assert judge_mod.task_success_rate(outcomes) == 50.0
    assert judge_mod.task_success_rate([]) is None
    report = judge_mod.format_judge_report(outcomes)
    assert "裁判失败（不计入）" in report


def test_run_judge_cases_end_to_end_with_a_fake_judge():
    """无 API Key 也能验证裁决接线：用离线夹具作答 + 假裁判打分。"""

    class FakeJudge:
        def __init__(self) -> None:
            self.seen: list[str] = []

        def judge(self, *, user_input: str, criteria: str, answer: str, observations=None):
            self.seen.append(answer)
            return judge_mod.JudgeVerdict(passed="出行助手" in answer, reason="含自我介绍")

    case = judge_mod.JudgeCase(
        case_id="judge_local",
        input="你是谁？",
        criteria="应说明自己是出行助手",
        offline=OfflineFixture(script=[{"text": "你好！我是你的出行助手。"}]),
    )
    fake = FakeJudge()

    outcomes = judge_mod.run_judge_cases(
        [case], offline.offline_engine_factory, fake  # type: ignore[arg-type]
    )

    assert fake.seen == ["你好！我是你的出行助手。"]
    assert outcomes[0].passed is True
    assert judge_mod.task_success_rate(outcomes) == 100.0


def test_run_judge_cases_turns_engine_crash_into_result():
    case = judge_mod.JudgeCase(case_id="boom", input="x", criteria="c")

    def failing_factory(_case: object):
        def engine(_inner: object) -> RunOutcome:
            raise RuntimeError("引擎炸了")

        return engine

    class NeverCalled:
        def judge(self, **_kwargs: object) -> None:  # pragma: no cover - 不应被调用
            raise AssertionError("引擎已失败，不应再调用裁判")

    outcomes = judge_mod.run_judge_cases([case], failing_factory, NeverCalled())  # type: ignore[arg-type]

    assert outcomes[0].error is not None
    assert outcomes[0].judged is False
