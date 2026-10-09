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

from first_agent.evals.case import (
    EvalCase,
    arguments_match,
    evaluate_assertion,
    memory_delta_matches,
    sequence_matches,
    set_matches,
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


Engine = Callable[[EvalCase], RunOutcome]


@dataclass
class CaseCheck:
    case: EvalCase
    outcome: RunOutcome
    checks: dict[str, bool | None] = field(default_factory=dict)
    assertion_results: list[bool] = field(default_factory=list)
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
        "tool_sequence": sequence_matches(case, outcome.tool_calls),
        "arguments": arguments_match(case, outcome.arguments),
        "memory_delta": memory_delta_matches(case, outcome.memory_actions),
    }
    assertion_results = [evaluate_assertion(a, outcome.answer) for a in case.assertions]
    return CaseCheck(
        case=case,
        outcome=outcome,
        checks=checks,
        assertion_results=assertion_results,
        error=outcome.error,
    )


def run_cases(cases: Iterable[EvalCase], engine: Engine) -> list[CaseCheck]:
    return [run_case(case, engine) for case in cases]
