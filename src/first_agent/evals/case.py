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

# 评测用例专属 user_id 前缀（design.md 16.4：用例之间必须隔离）
EVAL_USER_PREFIX = "case:"


class Assertion(BaseModel):
    """对最终答案的确定性断言（开放式答案另行使用 judge）。"""

    type: Literal["contains", "not_contains", "equals"] = "contains"
    target: str = "final_answer"
    value: str = ""


class OfflineFixture(BaseModel):
    """离线夹具：固定「一个称职的模型会怎么回答」（design.md 16.5）。

    稳定指标只反映 **Runtime 行为**（计划执行、依赖解析、参数透传、记忆裁决、
    失败降级），因此夹具必须完全确定；模型能力由真 LLM 段衡量。

    缺省字段按 ``expected_*`` 推导，只有推导不出来的部分才需要显式声明。
    """

    # 引擎部分的显式脚本（整段替换推导）；用于降级/重规划等特殊路径
    script: list[dict[str, Any]] | None = None
    # plan 模式的计划与槽位
    slots: dict[str, Any] = Field(default_factory=dict)
    plan: dict[str, Any] | None = None
    replan: dict[str, Any] | None = None
    # 最终回答（缺省由断言值拼出，保证 contains 断言可满足）
    final_answer: str = ""
    # Fake provider 数据
    weather: dict[str, dict[str, Any]] = Field(default_factory=dict)
    weather_failures: dict[str, str] = Field(default_factory=dict)
    # calendar 与 calendar_failures 的键可用 "$user" 表示该用例专属 user_id
    calendar: dict[str, dict[str, list[str]]] = Field(default_factory=dict)
    calendar_failures: dict[str, str] = Field(default_factory=dict)
    datetime_responses: dict[str, dict[str, Any]] = Field(default_factory=dict)
    travel_routes: dict[str, int] = Field(default_factory=dict)
    # 后置记忆：抽取结果 + 按需追加的裁决结果
    memory_extraction: dict[str, Any] | None = None
    memory_relations: list[dict[str, Any]] = Field(default_factory=list)


class EvalCase(BaseModel):
    case_id: str
    mode: Literal["react", "plan"]
    input: str
    # 用例类型（与 design.md 16.1 的分布表对照，便于按类型统计）
    category: str = ""
    initial_memory: dict[str, str] = Field(default_factory=dict)
    expected_tool_set: list[str] = Field(default_factory=list)
    expected_tool_sequence: list[str] = Field(default_factory=list)
    allowed_topological_orders: list[list[str]] = Field(default_factory=list)
    expected_arguments: dict[str, dict[str, Any]] = Field(default_factory=dict)
    expected_memory_delta: dict[str, str] = Field(default_factory=dict)
    max_steps: int | None = None
    expected_status: str | None = None
    # 默认**不允许**任何工具执行出错；只有「工具失败与降级」类用例才置 true。
    # 该判定独立于指标：否则工具全挂但工具集合/参数/答案都对时，指标会假装一切正常。
    expect_tool_errors: bool = False
    # 关键先后约束：``[["resolve_datetime", "get_weather"]]`` 表示前者必须出现在后者之前。
    # 只声明**语义上的必要顺序**，不追求唯一序列（ReAct 的顺序本来就不唯一）。
    precedence: list[list[str]] = Field(default_factory=list)
    assertions: list[Assertion] = Field(default_factory=list)
    offline: OfflineFixture | None = None

    def expected_call_count(self) -> int:
        """期望的工具调用次数（用于计算多余调用）。"""

        return len(self.expected_tool_sequence) or len(self.expected_tool_set)


def eval_user_id(case: EvalCase) -> str:
    """该用例专属的 ``user_id``（design.md 16.4：用例之间不得共享记忆）。"""

    return f"{EVAL_USER_PREFIX}{case.case_id}"


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

    # 加载期就把畸形夹具挡掉：否则会在跑用例时才抛错，炸掉整批（precedence_matches 里还有兜底）
    for case in cases:
        for pair in case.precedence:
            if len(pair) != 2 or pair[0] == pair[1]:
                raise ValueError(
                    f"{path} 用例 {case.case_id} 的 precedence 非法：{pair!r}"
                    "（必须是 [先, 后] 两个不同工具）"
                )
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
    """比较工具序列（仅 plan）；``allowed_topological_orders`` 优先。

    与 :func:`set_matches` 口径一致：**显式声明**才判定，
    ``"expected_tool_sequence": []`` 表示**不允许调用任何工具**。
    """

    if case.mode != "plan":
        return None
    if case.allowed_topological_orders:
        return observed in [list(order) for order in case.allowed_topological_orders]
    if "expected_tool_sequence" in case.model_fields_set:
        return observed == case.expected_tool_sequence
    return None


def required_tools(case: EvalCase) -> set[str] | None:
    """本用例的**必要工具**（声明了工具期望才有意义，否则返回 None = 不判定）。"""

    if (
        "expected_tool_set" not in case.model_fields_set
        and "expected_tool_sequence" not in case.model_fields_set
    ):
        return None
    return set(case.expected_tool_set) | set(case.expected_tool_sequence)


def tool_recall_ok(case: EvalCase, observed: list[str]) -> bool | None:
    """``Tool Set Accuracy``：是否**调用了所有必要工具**。

    只看召回、不惩罚多余调用（多余由 ``Extra Tool Call Rate`` 负责）——
    与 :func:`set_matches` 的「集合完全相等」是两件事，二者都要有：
    前者回答「该做的做了吗」，后者回答「有没有画蛇添足」。
    """

    required = required_tools(case)
    if required is None:
        return None
    return required <= set(observed)


def precedence_matches(case: EvalCase, observed: list[str]) -> bool | None:
    """``Precedence Accuracy``：只检查**声明的关键先后约束**。

    例如 ``resolve_datetime`` 必须在 ``get_weather`` 之前（日期要先解析出来）。
    未声明约束的用例返回 ``None``（不判定），避免把「顺序不唯一」当成错误。
    """

    if not case.precedence:
        return None
    for pair in case.precedence:
        if len(pair) != 2:
            raise ValueError(
                f"{case.case_id}: precedence 必须是 [先, 后] 二元组，收到 {pair!r}"
            )
        earlier, later = pair
        if earlier == later:
            raise ValueError(f"{case.case_id}: precedence 不能拿同一个工具作先后约束")
        if earlier not in observed or later not in observed:
            return False
        if observed.index(earlier) >= observed.index(later):
            return False
    return True


def arguments_match(case: EvalCase, observed: dict[str, list[dict[str, Any]]]) -> bool | None:
    """每个期望工具的期望参数，需在它的某一次调用中全部命中。

    只声明期望键即可（**超集也算命中**）：``user_id`` 这类由运行时注入的环境参数
    不属于模型输出，不应写进期望里——它的正确性由 ``get_calendar_events``
    类用例（夹具按用例专属 user_id 预置日程）间接验证。
    """

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
