"""仓库卫生护栏：本地数据库 / 备份 / 密钥绝不能进版本库。

背景（真实事故）：S5 曾把 `memory.db.v1.bak`（含真实用户记忆）提交进仓库，
原因是 `.gitignore` 写的是 `*.db.bak`，**匹配不到** `memory.db.v1.bak`。
规则一旦写窄，就会静默放行——所以这里用测试兜住。
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_SUFFIXES = (".db", ".bak", ".sqlite", ".sqlite3", ".db.bak")
FORBIDDEN_NAMES = {".env", "memory.db", "agent.db"}


def _tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=60
    )
    if result.returncode != 0:
        pytest.skip("不是 git 仓库，跳过仓库卫生检查")
    return [line for line in result.stdout.splitlines() if line.strip()]


def test_no_local_data_or_secrets_are_tracked():
    offenders = [
        path
        for path in _tracked_files()
        if path.endswith(FORBIDDEN_SUFFIXES) or Path(path).name in FORBIDDEN_NAMES
    ]

    assert offenders == [], f"以下本地数据或密钥被纳入了版本控制：{offenders}"


def test_gitignore_covers_versioned_backups():
    """回归用例：备份命名是 `*.v1.bak`，忽略规则必须用 `*.bak` 而不是 `*.db.bak`。"""

    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "*.bak" in gitignore.splitlines(), "根 .gitignore 未用 *.bak 覆盖版本化备份"
    assert "*.db.bak" not in gitignore.splitlines(), "*.db.bak 匹配不到 *.v1.bak，不应再使用"
