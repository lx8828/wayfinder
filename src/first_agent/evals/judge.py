"""LLM-as-Judge：开放式答案的判定（design.md 16.3 / 16.6）。

确定性答案用 ``assertions`` 断言；「天气和日程允许吗？」这类需要综合判断的开放式
答案无法用字符串断言，必须交给**独立裁判**打分，并单独报告 ``Task Success Rate``
（该指标只覆盖 judge 用例，确定性用例不计入）。

独立性的两层含义：
1. 独立的 system prompt（裁判不参与作答）；
2. 独立的结构化输出（裁判只回 ``{"passed": ..., "reason": ...}``）。

裁判失败的样本**不计入分母**（``judged=False``）——把裁判自身的波动混进任务成功率
会让指标既不可比也不可解释。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Sequence

from pydantic import BaseModel, Field

from first_agent.core.client import JSONValidationError, LLMClient
from first_agent.evals.case import OfflineFixture
from first_agent.evals.runner import RunOutcome

JUDGE_SYSTEM_PROMPT = """你是一个严格的评测裁判，负责判断助手回答是否满足给定的验收标准。

判定原则：
- 只依据「验收标准」判断，不因为文风、详略、语言差异扣分。
- 会一并给出**工具返回的观测数据**。回答中的数据若与观测一致，即视为可核实，
  不得判为编造；只有观测中不存在、且回答也无从推断的数据才算编造。
- 若观测数据为空（未调用工具），回答却给出了具体事实数据，判为编造。
- 回答若回避了标准中明确要求的内容（如漏掉日程冲突提示），判为不通过。
- 标准未要求的内容缺失，不扣分。

只返回 JSON：{"passed": true, "reason": "简要理由（不超过 60 字）"}
"""


class JudgeCase(BaseModel):
    """开放式用例：没有字符串断言，只有自然语言验收标准。"""

    case_id: str
    mode: Literal["react", "plan"] = "react"
    input: str
    criteria: str
    initial_memory: dict[str, str] = Field(default_factory=dict)
    max_steps: int | None = None
    # 仅供离线自测（真实运行由真模型作答，不使用该夹具）
    offline: OfflineFixture | None = None


class JudgeVerdict(BaseModel):
    passed: bool
    reason: str = ""


@dataclass
class JudgeOutcome:
    case: JudgeCase
    outcome: RunOutcome
    verdict: JudgeVerdict | None = None
    error: str | None = None

    @property
    def answer(self) -> str:
        return self.outcome.answer

    @property
    def judged(self) -> bool:
        return self.verdict is not None

    @property
    def passed(self) -> bool:
        return bool(self.verdict and self.verdict.passed)


def load_judge_cases(path: str | Path) -> list[JudgeCase]:
    cases: list[JudgeCase] = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            cases.append(JudgeCase.model_validate(json.loads(line)))
        except Exception as exc:  # noqa: BLE001 - 把行号带进错误信息
            raise ValueError(f"{path}:{line_number} judge 用例解析失败：{exc}") from exc
    ids = [case.case_id for case in cases]
    duplicates = sorted({case_id for case_id in ids if ids.count(case_id) > 1})
    if duplicates:
        raise ValueError(f"{path} 存在重复 case_id：{duplicates}")
    return cases


class LlmJudge:
    """裁判：只吃「输入 + 验收标准 + 助手回答」，产出结构化判定。"""

    def __init__(self, client: LLMClient, *, tracer: Any = None) -> None:
        self.client = client
        self.tracer = tracer
        self.calls = 0

    def judge(
        self,
        *,
        user_input: str,
        criteria: str,
        answer: str,
        observations: Sequence[dict[str, Any]] | None = None,
    ) -> JudgeVerdict | None:
        # 必须把观测数据给裁判：否则它会看到回答里的真实数值却无从核实，
        # 把「据实回答」误判成「编造事实」（首次真实运行就踩到了这一点）
        evidence = (
            json.dumps(list(observations), ensure_ascii=False)
            if observations
            else "（未调用工具，无观测数据）"
        )
        messages = [
            {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"用户输入：{user_input}\n"
                    f"验收标准：{criteria}\n"
                    f"工具返回的观测数据：{evidence}\n"
                    f"助手回答：{answer}\n"
                ),
            },
        ]
        self.calls += 1
        try:
            verdict = self.client.chat_json(messages, JudgeVerdict)
        except JSONValidationError as exc:
            if self.tracer is not None:
                self.tracer.log("judge_failed", error=str(exc))
            return None
        if self.tracer is not None:
            self.tracer.log("judged", passed=verdict.passed, reason=verdict.reason)
        return verdict


def run_judge_cases(
    cases: Sequence[JudgeCase],
    factory: Callable[[Any], Any],
    judge: LlmJudge,
) -> list[JudgeOutcome]:
    """逐条跑用例并请裁判打分；引擎异常转成结果，不让一条炸掉整批。"""

    results: list[JudgeOutcome] = []
    for case in cases:
        try:
            outcome = factory(case)(case)
        except Exception as exc:  # noqa: BLE001 - 评测需要把异常变成结果
            results.append(
                JudgeOutcome(
                    case=case,
                    outcome=RunOutcome(status="error"),
                    error=f"{type(exc).__name__}: {exc}",
                )
            )
            continue
        verdict = judge.judge(
            user_input=case.input,
            criteria=case.criteria,
            answer=outcome.answer,
            observations=outcome.observations,
        )
        results.append(JudgeOutcome(case=case, outcome=outcome, verdict=verdict))
    return results


def task_success_rate(outcomes: Sequence[JudgeOutcome]) -> float | None:
    """``Task Success Rate``：只统计**裁判成功**的样本；没有样本时返回 ``None``。"""

    judged = [item for item in outcomes if item.judged]
    if not judged:
        return None
    return round(100.0 * sum(item.passed for item in judged) / len(judged), 2)


def format_judge_report(outcomes: Sequence[JudgeOutcome]) -> str:
    lines = ["| 用例 | 判定 | 理由 |", "|---|---|---|"]
    for item in outcomes:
        if item.error:
            verdict, reason = "引擎异常", item.error
        elif not item.judged:
            verdict, reason = "裁判失败（不计入）", "结构化输出不合法"
        else:
            verdict = "通过" if item.passed else "不通过"
            reason = item.verdict.reason if item.verdict else ""
        lines.append(f"| {item.case.case_id} | {verdict} | {reason} |")
    return "\n".join(lines)
