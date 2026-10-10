"""长期记忆存储与**确定性**动作裁决。对应 design.md 12.1 / 12.3 / 12.4。

- v2.0 只支持单值 KV（``UNIQUE(user_id, key)``）
- LLM 只负责判断「新值与旧值的关系」（S5 的 Adjudicator），
  ``add / keep / update / skip`` 一律由本模块根据关系确定性决定
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from first_agent.core.db import Database, now_iso
from first_agent.schemas.memory import MemoryCandidate, UserProfile, decide_action

# 长期记忆键 → WorkingState 槽位（design.md 11.6 的地点解析优先级）
KEY_TO_SLOT: dict[str, str] = {
    "home_city": "destination",
    "transport_mode": "transport_mode",
}

MEMORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id      TEXT NOT NULL,
    key          TEXT NOT NULL,
    value        TEXT NOT NULL,
    category     TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'conversation',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    UNIQUE(user_id, key)
);
"""


@dataclass(frozen=True)
class MemoryRecord:
    user_id: str
    key: str
    value: str
    category: str
    source: str
    created_at: str
    updated_at: str
    last_seen_at: str


class MemoryStore:
    def __init__(self, db: Database) -> None:
        self.db = db
        db.script(MEMORY_SCHEMA)

    # -- 读 ---------------------------------------------------------------
    def get(self, user_id: str, key: str) -> MemoryRecord | None:
        row = self.db.query_one(
            "SELECT * FROM memories WHERE user_id = ? AND key = ?", (user_id, key)
        )
        return _to_record(row) if row else None

    def all(self, user_id: str) -> dict[str, MemoryRecord]:
        rows = self.db.query(
            "SELECT * FROM memories WHERE user_id = ? ORDER BY key", (user_id,)
        )
        return {row["key"]: _to_record(row) for row in rows}

    def values(self, user_id: str) -> dict[str, str]:
        return {key: record.value for key, record in self.all(user_id).items()}

    def users(self) -> list[str]:
        """有长期记忆的全部 ``user_id``（供 ``memory list`` 使用）。"""

        rows = self.db.query("SELECT DISTINCT user_id FROM memories ORDER BY user_id")
        return [row["user_id"] for row in rows]

    def profile(self, user_id: str) -> UserProfile:
        """生成只读的 ``UserProfile`` 视图（design.md 12.1）。"""

        return UserProfile.from_memories(self.values(user_id))

    # -- 写 ---------------------------------------------------------------
    def apply_candidate(self, candidate: MemoryCandidate, relation: str | None = None) -> str:
        """确定性裁决并落库，返回动作：``add`` / ``keep`` / ``update`` / ``skip``。

        - 无旧值（``relation is None``）→ ``add``，**不调用 Adjudicator**
        - 有旧值 → 由 ``relation`` 映射（``same→keep`` / ``conflict→update`` /
          ``complementary→skip`` / ``unsupported→skip``）
        """

        if relation is None:
            if self.get(candidate.user_id, candidate.key) is not None:
                # 调用方漏了旧值判断，按冲突处理，避免静默覆盖
                relation = "conflict"
            else:
                self._insert(candidate)
                return "add"

        action = decide_action(relation)
        if action == "keep":
            self.mark_seen(candidate.user_id, candidate.key)
        elif action == "update":
            self._update(candidate)
        return action

    def delete(self, user_id: str, key: str) -> bool:
        """删除一条记忆；返回是否真的删掉了（供 ``memory rm`` 使用）。"""

        if self.get(user_id, key) is None:
            return False
        self.db.execute("DELETE FROM memories WHERE user_id = ? AND key = ?", (user_id, key))
        return True

    def mark_seen(self, user_id: str, key: str) -> None:
        """``keep`` 时刷新 ``last_seen_at``（design.md 12.4 的字段行为）。"""

        self.db.execute(
            "UPDATE memories SET last_seen_at = ? WHERE user_id = ? AND key = ?",
            (now_iso(), user_id, key),
        )

    def import_candidate(self, candidate: MemoryCandidate) -> str:
        """**导入式**写入（迁移专用）：存在即覆盖，不做关系裁决。

        与 :meth:`apply_candidate` 的区别：这里假定调用方已经决定「就是要写这份数据」，
        因此永远 upsert；返回 ``add`` 或 ``update``。迁移必须幂等，所以不能走关系裁决。
        """

        if self.get(candidate.user_id, candidate.key) is None:
            self._insert(candidate)
            return "add"
        self._update(candidate)
        return "update"

    # -- 内部 -------------------------------------------------------------
    def _insert(self, candidate: MemoryCandidate, *, seen_at: str | None = None) -> None:
        timestamp = now_iso()
        self.db.execute(
            """
            INSERT INTO memories
                (user_id, key, value, category, source, created_at, updated_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.user_id,
                candidate.key,
                candidate.value,
                candidate.category,
                candidate.source,
                timestamp,
                timestamp,
                seen_at or timestamp,
            ),
        )

    def _update(self, candidate: MemoryCandidate, *, seen_at: str | None = None) -> None:
        timestamp = now_iso()
        self.db.execute(
            """
            UPDATE memories
            SET value = ?, category = ?, source = ?, updated_at = ?, last_seen_at = ?
            WHERE user_id = ? AND key = ?
            """,
            (
                candidate.value,
                candidate.category,
                candidate.source,
                timestamp,
                seen_at or timestamp,
                candidate.user_id,
                candidate.key,
            ),
        )


def _to_record(row: Mapping) -> MemoryRecord:
    return MemoryRecord(
        user_id=row["user_id"],
        key=row["key"],
        value=row["value"],
        category=row["category"],
        source=row["source"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_seen_at=row["last_seen_at"],
    )
