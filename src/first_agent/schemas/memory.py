"""长期记忆契约。

对应 design.md 第 12 节：

- v2.0 只支持单值 KV 记忆
- LLM 只判断「新值与旧值的关系」，``add/update/keep/skip`` 由代码确定性裁决
"""

from __future__ import annotations

from typing import Literal

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
