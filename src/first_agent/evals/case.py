"""离线评测用例模型、加载与判定。对应 design.md 16.2 / 16.3。

字段适用范围（design.md 16.2 表）：

- ``expected_tool_sequence`` / ``allowed_topological_orders``：用于 ``plan``
- ``expected_tool_set``：用于 ``react``（只比较集合）
- ``expected_arguments``：两者皆可，值可引用 ``initial_memory``

二者同时填写时以 ``allowed_topological_orders`` 为准。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

DEFAULT_GLOBAL_MAX_STEPS = 5
HARD_MAX_STEPS = 8


class Assertion(BaseModel):
    """对最终答案的确定性断言（开放式答案另行使用 judge）。"""

    type: Literal["contains", "not_contains", "equals"] = "contains"
    target: str = "final_answer"
    value: str = ""


class EvalCase(BaseModel):
    case_id: str
    mode: Literal["react", "plan"]
    input: str
    initial_memory: dict[str, str] = Field(default_factory=dict)
    expected_tool_set: list[str] = Field(default_factory=list)
    expected_tool_sequence: list[str] = Field(default_factory=list)
    allowed_topological_orders: list[list[str]] = Field(default_factory=list)
    expected_arguments: dict[str, dict[str, Any]] = Field(default_factory=dict)
    expected_memory_delta: dict[str, str] = Field(default_factory=dict)
    max_steps: int | None = None
    expected_status: str | None = None
    assertions: list[Assertion] = Field(default_factory=list)

    def expected_call_count(self) -> int:
        """期望的工具调用次数（用于计算多余调用）。"""

        return len(self.expected_tool_sequence) or len(self.expected_tool_set)


def load_cases(path: str | Path) -> list[EvalCase]:
    """从 JSONL 加载用例；空行忽略。"""

    cases: list[EvalCase] = []
    for line_number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            cases.append(EvalCase.model_validate(json.loads(line)))
        except Exception as exc:  # noqa: BLE001 - 需要把行号带进错误信息
            raise ValueError(f"{path}:{line_number} 用例解析失败：{exc}") from exc

    counts = Counter(case.case_id for case in cases)
    duplicates = sorted(case_id for case_id, count in counts.items() if count > 1)
    if duplicates:
        raise ValueError(f"{path} 存在重复 case_id：{duplicates}")
    return cases


def effective_max_steps(
    case: EvalCase,
    *,
    global_max_steps: int = DEFAULT_GLOBAL_MAX_STEPS,
    hard_cap: int = HARD_MAX_STEPS,
) -> int:
    """``min(case.max_steps or 全局值, 硬上限)``（design.md 14）。"""

    return min(case.max_steps or global_max_steps, hard_cap)


# -- 判定 ------------------------------------------------------------------


def set_matches(case: EvalCase, observed: list[str]) -> bool | None:
    """比较工具集合；未声明该字段时返回 None。

    用「字段是否被显式声明」区分「不约束」与「期望不调用工具」：
    JSON 里写 ``"expected_tool_set": []`` 表示**不允许调用任何工具**。
    """

    if "expected_tool_set" not in case.model_fields_set:
        return None
    return set(observed) == set(case.expected_tool_set)


def sequence_matches(case: EvalCase, observed: list[str]) -> bool | None:
    """比较工具序列（仅 plan）；``allowed_topological_orders`` 优先。"""

    if case.mode != "plan":
        return None
    if case.allowed_topological_orders:
        return observed in [list(order) for order in case.allowed_topological_orders]
    if case.expected_tool_sequence:
        return observed == case.expected_tool_sequence
    return None


def arguments_match(case: EvalCase, observed: dict[str, list[dict[str, Any]]]) -> bool | None:
    """每个期望工具的期望参数，需在它的某一次调用中全部命中。"""

    if not case.expected_arguments:
        return None
    for tool_name, expected in case.expected_arguments.items():
        calls = observed.get(tool_name)
        if not calls:
            return False
        hit = any(
            all(str(call.get(key)) == str(value) for key, value in expected.items())
            for call in calls
        )
        if not hit:
            return False
    return True


def memory_delta_matches(case: EvalCase, actions: dict[str, str]) -> bool | None:
    """比较记忆动作；未声明该字段时返回 None。

    ``expected_memory_delta`` 是**精确期望**（不是子集）：

    - ``{}`` 表示不允许产生任何记忆写入
    - 多出期望之外的动作同样判为失败
    """

    if "expected_memory_delta" not in case.model_fields_set:
        return None
    return dict(actions) == dict(case.expected_memory_delta)


def evaluate_assertion(assertion: Assertion, answer: str) -> bool:
    if assertion.type == "contains":
        return assertion.value in answer
    if assertion.type == "not_contains":
        return assertion.value not in answer
    return answer == assertion.value
