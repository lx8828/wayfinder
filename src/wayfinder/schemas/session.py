"""会话与任务状态契约。对应 design.md 8.3 / 9 / 10。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# WorkingState 中参与「多轮合并」的槽位字段
SLOT_FIELDS: tuple[str, ...] = (
    "destination",
    "origin",
    "date",
    "purpose",
    "transport_mode",
)


class WorkingState(BaseModel):
    """当前任务的槽位状态，由 SessionStore 跨轮持久化（不能每轮重置）。"""

    user_id: str
    session_id: str
    destination: str | None = None
    origin: str | None = None
    date: str | None = None
    purpose: str | None = None
    transport_mode: str | None = None
    pending_slots: list[str] = Field(default_factory=list)
    resolved_values: dict[str, Any] = Field(default_factory=dict)

    def fill(self, values: dict[str, Any]) -> "WorkingState":
        """用非空的槽位值覆盖自身（原地），返回自身便于链式调用。"""

        for name in SLOT_FIELDS:
            value = values.get(name)
            if value:
                setattr(self, name, value)
        if values.get("user_id"):
            self.user_id = values["user_id"]
        return self

    def merge(self, other: "WorkingState") -> "WorkingState":
        """以 ``other`` 的非空槽位覆盖 ``self``，用于多轮追问时合并上一轮状态。"""

        merged = self.model_copy(deep=True)
        merged.fill({name: getattr(other, name) for name in SLOT_FIELDS})
        merged.resolved_values.update(other.resolved_values)
        if other.pending_slots:
            merged.pending_slots = list(other.pending_slots)
        return merged


class SessionSummary(BaseModel):
    """会话摘要，覆盖到 ``covered_through_turn``（含）。"""

    session_id: str
    covered_through_turn: int = Field(ge=-1)
    summary: str
    token_count: int = Field(ge=0)
    updated_at: str = ""
