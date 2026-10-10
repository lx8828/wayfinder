"""显式契约层。

本层是最底层的依赖（design.md 第 6 节）：不允许 import runtime、tools、memory、core。
仅允许依赖 pydantic 与标准库。
"""

from wayfinder.schemas.memory import (
    CATEGORIES,
    RELATIONS,
    RELATION_TO_ACTION,
    MemoryCandidate,
    MemoryCategory,
    MemoryRelation,
    MemoryRelationKind,
    UserProfile,
    decide_action,
)
from wayfinder.schemas.plan import (
    REFERENCE_PREFIX,
    Plan,
    PlanStep,
    iter_references,
    parse_step_reference,
)
from wayfinder.schemas.session import SessionSummary, WorkingState
from wayfinder.schemas.tool import (
    NormalizedAssistantTurn,
    ToolCall,
    ToolResult,
    ToolSpec,
)

__all__ = [
    # tool
    "ToolCall",
    "ToolResult",
    "ToolSpec",
    "NormalizedAssistantTurn",
    # plan
    "Plan",
    "PlanStep",
    "REFERENCE_PREFIX",
    "parse_step_reference",
    "iter_references",
    # memory
    "MemoryCandidate",
    "MemoryCategory",
    "MemoryRelation",
    "MemoryRelationKind",
    "CATEGORIES",
    "RELATIONS",
    "RELATION_TO_ACTION",
    "decide_action",
    "UserProfile",
    # session
    "WorkingState",
    "SessionSummary",
]
