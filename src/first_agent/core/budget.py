"""单次 run 的延迟预算与超预算降级。对应 design.md 14.1。

设计要点：

- 预算是**硬性熔断点**：只拦「发起新调用」，进行中的调用允许收敛。
- 耗尽后**不允许**继续跑，也**不允许**静默抛异常——
  必须走「部分结果 + 说明」或「可解释的失败提示」两条降级路径之一。
- 判定以 **已用毫秒** 为准（不是调用次数），因此可注入 clock 做确定性测试。
"""

from __future__ import annotations

import json
import time
from typing import Callable, Sequence

from first_agent.schemas.tool import ToolResult

DEFAULT_BUDGET_MS = 60_000


class BudgetExceeded(RuntimeError):
    """预算耗尽。

    这是**引擎内部控制流信号**：引擎必须捕获它并转成 ``status="budget_exceeded"``
    的可解释结果，不允许冒泡到用户可见层。
    """

    def __init__(self, spent_ms: int, stage: str) -> None:
        super().__init__(f"耗时预算耗尽：已用 {spent_ms}ms，中断于 {stage}")
        self.spent_ms = spent_ms
        self.stage = stage


class RunBudget:
    """一次 run 共享的耗时预算。"""

    def __init__(
        self,
        *,
        budget_ms: int = DEFAULT_BUDGET_MS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        if budget_ms <= 0:
            raise ValueError("budget_ms 必须为正数")
        self.budget_ms = budget_ms
        self._clock = clock or time.perf_counter
        self._start = self._clock()

    @property
    def spent_ms(self) -> int:
        return int((self._clock() - self._start) * 1000)

    @property
    def remaining_ms(self) -> int:
        return max(0, self.budget_ms - self.spent_ms)

    @property
    def exhausted(self) -> bool:
        return self.spent_ms >= self.budget_ms

    def check(self, stage: str) -> None:
        """在**发起新调用之前**调用；耗尽则抛 :class:`BudgetExceeded`。"""

        spent = self.spent_ms
        if spent >= self.budget_ms:
            raise BudgetExceeded(spent, stage)


def check_budget(budget: RunBudget, stage: str) -> BudgetExceeded | None:
    """**不抛异常**的检查：返回 ``BudgetExceeded`` 供引擎走降级路径。

    引擎需要「停止循环 + 产出可解释回答」而不是让异常冒泡，所以这里把
    异常转成返回值。
    """

    try:
        budget.check(stage)
    except BudgetExceeded as exc:
        return exc
    return None


def _format_duration(ms: int) -> str:
    """小于 1 秒时按毫秒显示——不能把 1ms 的预算说成"1 秒"。"""

    return f"{ms} 毫秒" if ms < 1_000 else f"{round(ms / 1_000)} 秒"


def build_partial_answer(
    tool_results: Sequence[ToolResult],
    *,
    spent_ms: int,
    budget_ms: int,
    stage: str,
) -> str:
    """预算耗尽时的可解释降级回答（design.md 14.1 第 2、3 条）。

    有可用 Observation → 「部分结果 + 说明」；一条都没有 → 可解释的失败提示。
    """

    head = (
        f"本次处理已触发耗时预算（已用 {_format_duration(spent_ms)}，"
        f"上限 {_format_duration(budget_ms)}），已提前结束。"
    )

    usable = [item for item in tool_results if item.ok and item.data]
    if not usable:
        return f"抱歉，{head}目前没有取得可用的查询结果，请稍后重试或把问题拆小一些。"

    lines = [f"抱歉，{head}以下是我已经查到的部分信息（中断于 {stage}）："]
    for item in usable:
        lines.append(f"- {item.tool_name}：{json.dumps(item.data, ensure_ascii=False)}")
    lines.append("如需继续，请再问一次。")
    return "\n".join(lines)
