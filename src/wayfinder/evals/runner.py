"""离线评测执行骨架。对应 design.md 16.3 / 16.4 / 16.5。

本文件只做编排与判定，**不做任何网络调用**：

- 引擎通过 ``Engine`` 协议注入，真实引擎（ReActEngine / PlanExecuteEngine）
  在后续阶段接入；测试里用 ScriptedLLM + Fake provider 组装的引擎即可跑通用例。
- 每个用例的数据库隔离由引擎负责（design.md 16.4）：真实引擎接入 SessionStore 后，
  使用独立临时 SQLite 或 ``:memory:``，禁止在用例之间共享 memory.db。
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from wayfinder.evals.case import (
    EvalCase,
    arguments_match,
    assertion_label,
    evaluate_assertion,
    memory_delta_matches,
    precedence_matches,
    sequence_matches,
    set_matches,
    tool_recall_ok,
)


@dataclass
class RunOutcome:
    """一次 run 的可评测结果，由引擎产出。"""

    answer: str = ""
    tool_calls: list[str] = field(default_factory=list)
    arguments: dict[str, list[dict]] = field(default_factory=dict)
    steps: int = 0
    latency_ms: int = 0
    status: str = "success"
    memory_actions: dict[str, str] = field(default_factory=dict)
    structured_output_ok: bool = True
    error: str | None = None
    # 计划是否通过校验（``Plan Validity Rate`` 的「依赖 DAG 合法」一项）；
    # ReAct 模式或未上报时为 None = 不判定
    plan_valid: bool | None = None
    # 执行失败的调用（含被依赖失败阻塞的步骤），供「工具必须真的成功」这一判定使用
    tool_errors: list[dict[str, Any]] = field(default_factory=list)
    # 成功调用的观测数据：裁判需要它才能判断「回答里的数字是不是编造的」，
    # 断言也需要它来做**结构化检查**（关键实体/数值/日期的正确来源）
    observations: list[dict[str, Any]] = field(default_factory=list)
    # 引擎结构化给出的待补槽位（追问类用例断言这里，而不是中文回答里的英文 slot 名）
    need_info: list[str] = field(default_factory=list)


Engine = Callable[[EvalCase], RunOutcome]
# 用例 → 引擎：允许为**每个用例**单独构造（design.md 16.4 的 DB 隔离要求）
EngineFactory = Callable[[EvalCase], Engine]


@dataclass
class CaseCheck:
    case: EvalCase
    outcome: RunOutcome
    checks: dict[str, bool | None] = field(default_factory=dict)
    assertion_results: list[bool] = field(default_factory=list)
    # 失败断言的可读标签（失败报告据此定位「尺子是不是又太硬」）
    assertion_failures: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def extra_calls(self) -> int:
        """超出期望的工具调用次数（把重复调用也算多余）。"""

        return max(0, len(self.outcome.tool_calls) - self.case.expected_call_count())

    @property
    def passed(self) -> bool:
        if self.error:
            return False
        if any(value is False for value in self.checks.values()):
            return False
        if self.assertion_results and not all(self.assertion_results):
            return False
        if self.case.expected_status and self.outcome.status != self.case.expected_status:
            return False
        return True


def tool_calls_ok(case: EvalCase, outcome: RunOutcome) -> bool | None:
    """工具是否**真的全部执行成功**。

    「工具失败与降级」类用例声明 ``expect_tool_errors`` 后不再判定；其余用例只要
    有一次调用失败即判失败——否则工具全挂、但工具集合与参数恰好都对时，
    指标会假装一切正常（这是本套件最初版本的漏洞）。
    """

    if case.expect_tool_errors:
        return None
    return not outcome.tool_errors


def plan_validity_ok(
    case: EvalCase, outcome: RunOutcome, checks: dict[str, bool | None]
) -> bool | None:
    """``Plan Validity Rate``：工具集合正确 + 依赖 DAG 合法 + 无失败工具 + 无多余调用。

    四项缺一不可，且只对 **plan** 用例判定（ReAct 没有"计划"这个概念）。
    没声明工具期望、或引擎没上报 ``plan_valid`` 时返回 ``None``：不臆断。

    「工具失败与降级」类用例（``expect_tool_errors=True``）也返回 ``None``：
    它们**故意**注入工具失败来验证降级路径，拿"无失败工具"去判它们，
    衡量的是夹具而不是 Runtime（与 :func:`tool_calls_ok` 同一口径）。
    """

    if case.mode != "plan" or case.expect_tool_errors:
        return None
    declared = checks.get("tool_set")
    if declared is None:
        declared = checks.get("tool_sequence")
    if declared is None or outcome.plan_valid is None:
        return None
    extra_calls = max(0, len(outcome.tool_calls) - case.expected_call_count())
    return (
        bool(declared)
        and bool(outcome.plan_valid)
        and not outcome.tool_errors
        and extra_calls == 0
    )


def plan_success_ok(
    case: EvalCase, checks: dict[str, bool | None], assertions_ok: bool | None
) -> bool | None:
    """``Plan Success Rate``：计划合法**且**最终结果通过断言。

    未声明断言的用例按"不扣分"处理（``None`` 不算失败），因为该指标衡量的是
    「计划合法 + 结果可用」，而不是「必须写了断言」。
    """

    validity = checks.get("plan_validity")
    if validity is None:
        return None
    return bool(validity) and assertions_ok is not False


def run_case(case: EvalCase, engine: Engine) -> CaseCheck:
    """执行单个用例并把异常转成结果，避免一个用例炸掉整批。"""

    started = time.perf_counter()
    try:
        outcome = engine(case)
    except Exception as exc:  # noqa: BLE001 - 评测需要把异常变成结果
        outcome = RunOutcome(status="error", error=f"{type(exc).__name__}: {exc}")
    if outcome.latency_ms == 0:
        outcome.latency_ms = int((time.perf_counter() - started) * 1000)

    checks: dict[str, bool | None] = {
        "tool_set": set_matches(case, outcome.tool_calls),
        "tool_recall": tool_recall_ok(case, outcome.tool_calls),
        "tool_sequence": sequence_matches(case, outcome.tool_calls),
        "precedence": precedence_matches(case, outcome.tool_calls),
        "arguments": arguments_match(case, outcome.arguments),
        "memory_delta": memory_delta_matches(case, outcome.memory_actions),
        "tool_ok": tool_calls_ok(case, outcome),
    }
    assertion_results = [evaluate_assertion(a, outcome) for a in case.assertions]
    assertion_failures = [
        assertion_label(assertion)
        for assertion, ok in zip(case.assertions, assertion_results)
        if not ok
    ]
    checks["plan_validity"] = plan_validity_ok(case, outcome, checks)
    checks["plan_success"] = plan_success_ok(
        case, checks, all(assertion_results) if assertion_results else None
    )
    return CaseCheck(
        case=case,
        outcome=outcome,
        checks=checks,
        assertion_results=assertion_results,
        assertion_failures=assertion_failures,
        error=outcome.error,
    )


def run_cases(cases: Iterable[EvalCase], engine: Engine) -> list[CaseCheck]:
    return [run_case(case, engine) for case in cases]


def run_cases_isolated(cases: Iterable[EvalCase], factory: EngineFactory) -> list[CaseCheck]:
    """每个用例**单独构造**引擎与数据库（design.md 16.4）。

    共享引擎会让记忆与会话在用例之间互相污染，因此真实评测必须走这条路。
    """

    return [run_case(case, factory(case)) for case in cases]
