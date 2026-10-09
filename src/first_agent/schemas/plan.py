"""Plan / PlanStep 契约与 ``$steps.<id>.<field>`` 引用解析。

对应 design.md 8.1：只保留 ``depends_on`` + ``$steps.<id>.<field>`` 一套寻址，
不再有 ``output_key``。
"""

from __future__ import annotations

import re
from typing import Any, Iterator

from pydantic import BaseModel, Field

REFERENCE_PREFIX = "$steps."

_REFERENCE_RE = re.compile(r"^\$steps\.(\d+)\.([A-Za-z_][A-Za-z0-9_]*)$")


class PlanStep(BaseModel):
    """计划中的一步：一个工具 + 显式参数 + 依赖。"""

    id: int = Field(ge=0)
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[int] = Field(default_factory=list)


class Plan(BaseModel):
    """规划结果；执行顺序由 ``depends_on`` 拓扑排序得到，不依赖列表顺序。"""

    need_info: list[str] = Field(default_factory=list)
    steps: list[PlanStep] = Field(default_factory=list)
    reason: str = ""

    def step_ids(self) -> set[int]:
        return {step.id for step in self.steps}

    def duplicate_ids(self) -> list[int]:
        """返回重复出现的 step id（升序）。"""

        seen: set[int] = set()
        duplicates: set[int] = set()
        for step in self.steps:
            if step.id in seen:
                duplicates.add(step.id)
            seen.add(step.id)
        return sorted(duplicates)


def parse_step_reference(value: Any) -> tuple[int, str] | None:
    """解析 ``$steps.<id>.<field>``。

    返回 ``(step_id, field)``；不是引用返回 ``None``；
    以 ``$steps.`` 开头但格式非法则抛 ``ValueError``（让非法引用尽早暴露）。
    """

    if not isinstance(value, str) or not value.startswith(REFERENCE_PREFIX):
        return None
    match = _REFERENCE_RE.match(value)
    if not match:
        raise ValueError(f"非法的步骤引用：{value!r}（期望 $steps.<id>.<field>）")
    return int(match.group(1)), match.group(2)


def iter_references(arguments: dict[str, Any]) -> Iterator[tuple[int, str]]:
    """产出 arguments（含嵌套 dict / list）中的全部引用。"""

    for value in arguments.values():
        if isinstance(value, dict):
            yield from iter_references(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    yield from iter_references(item)
                else:
                    reference = parse_step_reference(item)
                    if reference is not None:
                        yield reference
        else:
            reference = parse_step_reference(value)
            if reference is not None:
                yield reference
