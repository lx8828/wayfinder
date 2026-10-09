"""SessionStore / SessionResolver。对应 design.md 8.3 / 9 / 10。

四张表：``sessions`` / ``working_states`` / ``turns`` / ``summaries``。
所有查询都带 ``session_id``，避免跨会话串数据（design 16.4）。

注意：``WorkingState`` 必须**跨轮保留**（v1 每轮清零是已知缺陷）。
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from first_agent.core.db import Database, now_iso
from first_agent.schemas.session import SessionSummary, WorkingState

RECENT_TURNS_DEFAULT = 8

SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS working_states (
    session_id TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS turns (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       TEXT NOT NULL,
    turn_index       INTEGER NOT NULL,
    user_input       TEXT NOT NULL,
    assistant_output TEXT NOT NULL,
    messages_json    TEXT,
    created_at       TEXT NOT NULL,
    UNIQUE(session_id, turn_index)
);

CREATE TABLE IF NOT EXISTS summaries (
    session_id           TEXT PRIMARY KEY,
    user_id              TEXT NOT NULL,
    covered_through_turn INTEGER NOT NULL,
    summary              TEXT NOT NULL,
    token_count          INTEGER NOT NULL,
    updated_at           TEXT NOT NULL
);
"""


@dataclass(frozen=True)
class SessionRef:
    user_id: str
    session_id: str


class SessionResolver:
    """解析 ``user_id / session_id``，缺省时生成新会话。"""

    def __init__(self, *, id_factory: Callable[[], str] | None = None, default_user: str = "anonymous") -> None:
        self._id_factory = id_factory or (lambda: f"s_{uuid.uuid4().hex[:12]}")
        self.default_user = default_user

    def resolve(self, *, user_id: str | None = None, session_id: str | None = None) -> SessionRef:
        return SessionRef(
            user_id=(user_id or self.default_user).strip() or self.default_user,
            session_id=(session_id or self._id_factory()).strip() or self._id_factory(),
        )


class SessionStore:
    def __init__(self, db: Database) -> None:
        self.db = db
        db.script(SESSION_SCHEMA)

    # -- 会话 -------------------------------------------------------------
    def ensure_session(self, ref: SessionRef) -> None:
        timestamp = now_iso()
        self.db.execute(
            """
            INSERT INTO sessions (session_id, user_id, created_at, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET updated_at = excluded.updated_at
            """,
            (ref.session_id, ref.user_id, timestamp, timestamp),
        )

    # -- WorkingState -----------------------------------------------------
    def load_working_state(self, ref: SessionRef) -> WorkingState:
        row = self.db.query_one(
            "SELECT state_json FROM working_states WHERE session_id = ?", (ref.session_id,)
        )
        if row is None:
            return WorkingState(user_id=ref.user_id, session_id=ref.session_id)
        return WorkingState.model_validate_json(row["state_json"])

    def save_working_state(self, state: WorkingState) -> None:
        self.db.execute(
            """
            INSERT INTO working_states (session_id, user_id, state_json, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                state_json = excluded.state_json,
                updated_at = excluded.updated_at
            """,
            (state.session_id, state.user_id, state.model_dump_json(), now_iso()),
        )

    # -- 轮次 -------------------------------------------------------------
    def turn_count(self, ref: SessionRef) -> int:
        row = self.db.query_one(
            "SELECT COUNT(*) AS total FROM turns WHERE session_id = ?", (ref.session_id,)
        )
        return int(row["total"]) if row else 0

    def append_turn(
        self,
        ref: SessionRef,
        *,
        user_input: str,
        assistant_output: str,
        messages: Sequence[dict] | None = None,
    ) -> int:
        turn_index = self.turn_count(ref)
        self.db.execute(
            """
            INSERT INTO turns
                (session_id, turn_index, user_input, assistant_output, messages_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                ref.session_id,
                turn_index,
                user_input,
                assistant_output,
                json.dumps(list(messages), ensure_ascii=False) if messages else None,
                now_iso(),
            ),
        )
        return turn_index

    def recent_turns(self, ref: SessionRef, limit: int = RECENT_TURNS_DEFAULT) -> list[dict]:
        """最近 ``limit`` 轮的完整消息（含 tool_calls / observation），旧→新。"""

        if limit <= 0:
            return []
        rows = self.db.query(
            """
            SELECT messages_json, user_input, assistant_output
            FROM turns WHERE session_id = ?
            ORDER BY turn_index DESC LIMIT ?
            """,
            (ref.session_id, limit),
        )
        messages: list[dict] = []
        for row in reversed(rows):
            payload = row["messages_json"]
            if payload:
                try:
                    parsed = json.loads(payload)
                except ValueError:
                    parsed = None
                if isinstance(parsed, list) and parsed:
                    messages.extend(
                        dict(message) for message in parsed if message.get("role") != "system"
                    )
                    continue
            messages.append({"role": "user", "content": row["user_input"]})
            messages.append({"role": "assistant", "content": row["assistant_output"]})
        return messages

    # -- 摘要 -------------------------------------------------------------
    def load_summary(self, ref: SessionRef) -> SessionSummary | None:
        row = self.db.query_one(
            "SELECT * FROM summaries WHERE session_id = ?", (ref.session_id,)
        )
        if row is None:
            return None
        return SessionSummary(
            session_id=row["session_id"],
            covered_through_turn=int(row["covered_through_turn"]),
            summary=row["summary"],
            token_count=int(row["token_count"]),
            updated_at=row["updated_at"],
        )

    def save_summary(self, summary: SessionSummary) -> None:
        self.db.execute(
            """
            INSERT INTO summaries
                (session_id, user_id, covered_through_turn, summary, token_count, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                covered_through_turn = excluded.covered_through_turn,
                summary = excluded.summary,
                token_count = excluded.token_count,
                updated_at = excluded.updated_at
            """,
            (
                summary.session_id,
                # user_id 归属由会话表决定；这里从与会话表一致的来源写入
                self._session_user(summary.session_id),
                summary.covered_through_turn,
                summary.summary,
                summary.token_count,
                summary.updated_at or now_iso(),
            ),
        )

    # -- 内部 -------------------------------------------------------------
    def _session_user(self, session_id: str) -> str:
        row = self.db.query_one(
            "SELECT user_id FROM sessions WHERE session_id = ?", (session_id,)
        )
        return str(row["user_id"]) if row else "anonymous"
