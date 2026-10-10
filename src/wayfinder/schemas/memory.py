"""长期记忆契约。

对应 design.md 第 12 节：

- v2.0 只支持单值 KV 记忆
- LLM 只判断「新值与旧值的关系」，``add/update/keep/skip`` 由代码确定性裁决
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Mapping

from pydantic import BaseModel, Field

MemoryCategory = Literal["user_identity", "preference", "habit", "long_term_plan"]
MemoryRelationKind = Literal["same", "conflict", "complementary", "unsupported"]

CATEGORIES: tuple[str, ...] = ("user_identity", "preference", "habit", "long_term_plan")
RELATIONS: tuple[str, ...] = ("same", "conflict", "complementary", "unsupported")

# LLM 关系 -> 数据库动作 的确定性映射（design.md 12.3）
RELATION_TO_ACTION: dict[str, str] = {
    "same": "keep",
    "conflict": "update",
    "complementary": "skip",
    "unsupported": "skip",
}


class MemoryCandidate(BaseModel):
    """Extractor 的输出：一条值得长期保存的信息。"""

    user_id: str
    key: str
    value: str
    category: MemoryCategory
    source: str = "conversation"


class MemoryRelation(BaseModel):
    """Adjudicator 的输出：新值与同 key 旧值的关系。"""

    relation: MemoryRelationKind
    reason: str = ""


def decide_action(relation: str) -> str:
    """把关系确定性地映射为数据库动作。

    未知关系一律 ``skip``（不猜测、不静默写入），由调用方记录 Trace。
    """

    return RELATION_TO_ACTION.get(relation, "skip")


@dataclass(frozen=True)
class UserProfile:
    """长期记忆的**只读视图**（design.md 12.1）。

    记忆本身是 KV，``home_city`` 是 key 而不是对象属性；需要 typed 访问时
    用本视图，避免把结构假设写进存储层。
    """

    memories: Mapping[str, str] = field(default_factory=dict)

    @classmethod
    def from_memories(cls, memories: Mapping[str, str]) -> "UserProfile":
        return cls(dict(memories))

    def get(self, key: str, default: str | None = None) -> str | None:
        return self.memories.get(key, default)

    @property
    def home_city(self) -> str | None:
        return self.memories.get("home_city")

    @property
    def user_name(self) -> str | None:
        return self.memories.get("user_name")

    @property
    def timezone(self) -> str | None:
        return self.memories.get("timezone")

    @property
    def transport_mode(self) -> str | None:
        return self.memories.get("transport_mode")

    @property
    def dietary_preference(self) -> str | None:
        return self.memories.get("dietary_preference")

    @property
    def long_term_plan(self) -> str | None:
        return self.memories.get("long_term_plan")

    def is_empty(self) -> bool:
        return not self.memories

    def as_items(self) -> list[tuple[str, str]]:
        return sorted(self.memories.items())
