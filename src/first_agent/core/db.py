"""通用 SQLite 封装。

**不含任何业务 schema**：DDL 由使用方（``SessionStore`` / ``MemoryStore``）提供。
``core`` 是共享基础设施，``memory`` 与 ``runtime`` 都可以依赖它。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Sequence

IN_MEMORY = ":memory:"


def now_iso() -> str:
    """统一的 ISO 时间戳（秒精度，便于比较与测试）。"""

    return datetime.now().isoformat(timespec="seconds")


class Database:
    """薄封装：连接复用、schema 初始化与事务。"""

    def __init__(self, path: str | Path = IN_MEMORY) -> None:
        self.path = str(path)
        self._connection: sqlite3.Connection | None = None

    # -- 连接 -------------------------------------------------------------
    @property
    def connection(self) -> sqlite3.Connection:
        if self._connection is None:
            # check_same_thread=False：Flask/线程池场景下连接可被其他线程使用
            self._connection = sqlite3.connect(self.path, check_same_thread=False)
            self._connection.row_factory = sqlite3.Row
        return self._connection

    def script(self, ddl: str) -> None:
        """执行建表脚本（``executescript`` 自带提交）。"""

        self.connection.executescript(ddl)
        self.connection.commit()

    # -- 读写 -------------------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        cursor = self.connection.execute(sql, tuple(params))
        try:
            return cursor.fetchall()
        finally:
            cursor.close()

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self.transaction() as cursor:
            cursor.execute(sql, tuple(params))
            return cursor.rowcount

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Cursor]:
        connection = self.connection
        cursor = connection.cursor()
        try:
            yield cursor
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            cursor.close()

    # -- 生命周期 ---------------------------------------------------------
    def close(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
