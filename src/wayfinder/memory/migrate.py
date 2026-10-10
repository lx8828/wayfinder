"""v1 → v2 长期记忆迁移。对应 design.md 18.2。

- 迁移前**备份**原库为 `memory.db.v1.bak`，原文件不删除、只读打开
- 中文 key/category → v2 规范键值
- **未知 key 原样保留并记 WARN**：迁移不能丢用户既有数据
  （这与 Extractor 对未知 key「丢弃」的策略**刻意不同**——那是在产生新数据，
  这里是保住老数据）
- 幂等：重复执行结果一致

逻辑放在包内以便测试；`scripts/migrate_memory_v1.py` 只是薄 CLI 包装。
"""

from __future__ import annotations

import shutil
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from wayfinder.core.db import Database
from wayfinder.memory.extractor import CANONICAL_KEYS, CATEGORY_ALIASES, KEY_ALIASES
from wayfinder.memory.store import MemoryStore
from wayfinder.schemas.memory import CATEGORIES, MemoryCandidate

# design.md 18.2 的 key 映射表
# 注：「饮食偏好」是 S5 用真实 v1 库验证时补上的——原表漏了这一项，
# 而 v1 实际数据里确实存在，若不补会以 WARN 形式原样保留（可用但不规范）。
KEY_MAP: dict[str, str] = {
    "居住地": "home_city",
    "用户城市": "home_city",
    "用户姓名": "user_name",
    "姓名": "user_name",
    "出行偏好": "transport_mode",
    "饮食偏好": "dietary_preference",
    "长期计划": "long_term_plan",
}

# design.md 18.2 的 category 映射表（真实数据里出现过「用户偏好」）
CATEGORY_MAP: dict[str, str] = {
    "用户身份": "user_identity",
    "用户偏好": "preference",
    "偏好": "preference",
    "习惯": "habit",
    "长期计划": "long_term_plan",
}

DEFAULT_CATEGORY = "preference"
V1_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL,
    category TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

DEFAULT_V1_PATH = "archive/handwritten_v1/src/wayfinder/memory/memory.db"
DEFAULT_V2_PATH = "var/agent.db"


@dataclass
class MigrationReport:
    total: int = 0
    added: int = 0
    updated: int = 0
    # 规范化（预期内的重命名）与 warnings（真异常）分开，避免警告被噪音淹没
    renamed_keys: list[tuple[str, str]] = field(default_factory=list)
    renamed_categories: list[tuple[str, str]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    backup_path: str | None = None

    def summary(self) -> str:
        lines = [
            f"读取 v1 记忆 {self.total} 条",
            f"写入 v2：新增 {self.added} 条 / 覆盖 {self.updated} 条",
        ]
        if self.backup_path:
            lines.append(f"原库备份：{self.backup_path}")
        if self.renamed_keys:
            lines.append("key 规范化：" + _format_mappings(self.renamed_keys))
        if self.renamed_categories:
            lines.append("category 规范化：" + _format_mappings(self.renamed_categories))
        lines.extend(f"WARN: {warning}" for warning in self.warnings)
        return "\n".join(lines)


def _format_mappings(pairs: list[tuple[str, str]]) -> str:
    """去重并保持首次出现顺序（同一条映射可能命中多次）。"""

    return "、".join(f"{old}→{new}" for old, new in dict.fromkeys(pairs))


def read_v1_memories(path: str | Path) -> list[dict[str, Any]]:
    """只读方式读取 v1 的 memories 表（不修改原文件）。"""

    file = Path(path)
    if not file.exists():
        raise FileNotFoundError(f"找不到 v1 数据库：{file}")

    connection = sqlite3.connect(f"file:{file}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        rows = connection.execute("SELECT key, value, category FROM memories").fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def map_key(key: str) -> tuple[str, str]:
    """返回 ``(v2_key, status)``，status ∈ ``canonical`` / ``renamed`` / ``unknown``。"""

    cleaned = (key or "").strip()
    if cleaned in CANONICAL_KEYS:
        return cleaned, "canonical"
    if cleaned in KEY_MAP:
        return KEY_MAP[cleaned], "renamed"
    if cleaned in KEY_ALIASES:
        return KEY_ALIASES[cleaned], "renamed"
    return cleaned, "unknown"


def map_category(category: Any) -> tuple[str, str]:
    """返回 ``(v2_category, status)``，status ∈ ``canonical`` / ``renamed`` / ``unknown``。"""

    cleaned = (category or "").strip()
    if cleaned in CATEGORIES:
        return cleaned, "canonical"
    if cleaned in CATEGORY_MAP:
        return CATEGORY_MAP[cleaned], "renamed"
    if cleaned in CATEGORY_ALIASES:
        return CATEGORY_ALIASES[cleaned], "renamed"
    return DEFAULT_CATEGORY, "unknown"


def migrate(
    v1_path: str | Path,
    v2_path: str | Path,
    *,
    user_id: str = "anonymous",
    backup: bool = True,
) -> MigrationReport:
    """把 v1 记忆导入 v2 库；可重复执行（幂等）。"""

    report = MigrationReport()
    v1_file = Path(v1_path)
    rows = read_v1_memories(v1_file)
    report.total = len(rows)

    if backup:
        backup_path = v1_file.with_name(v1_file.name + ".v1.bak")
        shutil.copy2(v1_file, backup_path)
        report.backup_path = str(backup_path)

    v2_file = Path(v2_path)
    v2_file.parent.mkdir(parents=True, exist_ok=True)

    db = Database(v2_file)
    try:
        store = MemoryStore(db)
        for row in rows:
            key, key_status = map_key(row["key"])
            if not key:
                report.warnings.append(f"空 key 已跳过：value={row['value']!r}")
                continue
            if key_status == "renamed":
                report.renamed_keys.append((row["key"], key))
            elif key_status == "unknown":
                report.warnings.append(f"未识别的 key 原样保留：{key}")

            category, category_status = map_category(row["category"])
            if category_status == "renamed":
                report.renamed_categories.append((row["category"], category))
            elif category_status == "unknown":
                report.warnings.append(
                    f"未识别的 category {row['category']!r}，已回落为 {category}"
                )

            value = (row["value"] or "").strip()
            if not value:
                report.warnings.append(f"空 value 已跳过：key={key}")
                continue

            action = store.import_candidate(
                MemoryCandidate(
                    user_id=user_id,
                    key=key,
                    value=value,
                    category=category,
                    source="migration_v1",
                )
            )
            if action == "add":
                report.added += 1
            else:
                report.updated += 1
    finally:
        db.close()

    return report
