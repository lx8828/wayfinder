"""v1 `memory.db` → v2 一次性迁移（CLI 薄包装）。对应 design.md 18.2。

用法：

    uv run python scripts/migrate_memory_v1.py
    uv run python scripts/migrate_memory_v1.py --v1 archive/handwritten_v1/src/first_agent/memory/memory.db --v2 var/agent.db --user-id demo
    uv run python scripts/migrate_memory_v1.py --no-backup

真正的逻辑在 `first_agent.memory.migrate`（便于单元测试）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from first_agent.memory.migrate import (  # noqa: E402
    DEFAULT_V1_PATH,
    DEFAULT_V2_PATH,
    migrate,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="迁移 v1 长期记忆到 v2")
    parser.add_argument("--v1", default=DEFAULT_V1_PATH, help="v1 memory.db 路径")
    parser.add_argument("--v2", default=DEFAULT_V2_PATH, help="v2 SQLite 路径")
    parser.add_argument("--user-id", default="demo", help="写入 v2 时使用的 user_id")
    parser.add_argument("--no-backup", action="store_true", help="不备份原库")
    args = parser.parse_args(argv)

    try:
        report = migrate(args.v1, args.v2, user_id=args.user_id, backup=not args.no_backup)
    except FileNotFoundError as exc:
        print(f"[migrate] {exc}", file=sys.stderr)
        return 2

    print(report.summary())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
