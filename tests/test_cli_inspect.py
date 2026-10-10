"""S8：``memory`` / ``trace`` 子命令与运维视图（design.md 15 / Phase 5）。"""

from __future__ import annotations

import json

import pytest

from first_agent.core import inspect as inspect_mod
from first_agent.core.db import Database
from first_agent.core.tracing import TraceLogger
from first_agent.main import main
from first_agent.memory.store import MemoryStore
from first_agent.schemas.memory import MemoryCandidate


@pytest.fixture
def seeded_db(tmp_path, monkeypatch):
    """准备一个有记忆的库，并把 CLI 指向它。"""

    db_path = tmp_path / "agent.db"
    monkeypatch.setenv("FIRST_AGENT_DB", str(db_path))
    with Database(db_path) as db:
        store = MemoryStore(db)
        store.apply_candidate(
            MemoryCandidate(user_id="u1", key="home_city", value="重庆", category="user_identity")
        )
        store.apply_candidate(
            MemoryCandidate(user_id="u1", key="user_name", value="张三", category="user_identity")
        )
        store.apply_candidate(
            MemoryCandidate(user_id="u2", key="home_city", value="杭州", category="user_identity")
        )
    return db_path


# -- memory ----------------------------------------------------------------


def test_memory_list_shows_the_users_memories(seeded_db, capsys):
    assert main(["memory", "list", "--user", "u1"]) == 0

    out = capsys.readouterr().out
    assert "用户 u1（2 条）" in out
    assert "home_city" in out and "重庆" in out
    assert "杭州" not in out  # 只显示指定用户


def test_memory_list_all_users(seeded_db, capsys):
    assert main(["memory", "list", "--all-users"]) == 0

    out = capsys.readouterr().out
    assert "u1" in out and "u2" in out


def test_memory_list_json_output(seeded_db, capsys):
    assert main(["memory", "list", "--user", "u1", "--json"]) == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["u1"][0]["key"] == "home_city"
    assert {item["value"] for item in payload["u1"]} == {"重庆", "张三"}


def test_memory_show_and_rm_roundtrip(seeded_db, capsys):
    assert main(["memory", "show", "home_city", "--user", "u1"]) == 0
    assert "重庆" in capsys.readouterr().out

    assert main(["memory", "rm", "home_city", "--user", "u1"]) == 0
    assert "已删除" in capsys.readouterr().out

    assert main(["memory", "show", "home_city", "--user", "u1"]) == 1
    assert "没有记忆" in capsys.readouterr().err


def test_memory_rm_missing_key_returns_one(seeded_db, capsys):
    assert main(["memory", "rm", "nope", "--user", "u1"]) == 1
    assert "不存在" in capsys.readouterr().out


def test_memory_show_without_key_is_usage_error(seeded_db, capsys):
    assert main(["memory", "show"]) == 2
    assert "需要指定 key" in capsys.readouterr().err


def test_memory_list_without_database_is_empty_not_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FIRST_AGENT_DB", str(tmp_path / "missing.db"))

    assert main(["memory", "list", "--user", "u1"]) == 0
    assert "还没有任何记忆" in capsys.readouterr().out


def test_delete_is_idempotent(seeded_db):
    with Database(seeded_db) as db:
        store = MemoryStore(db)
        assert store.delete("u1", "home_city") is True
        assert store.delete("u1", "home_city") is False
        assert store.users() == ["u1", "u2"]
        assert store.values("u1") == {"user_name": "张三"}


# -- trace -----------------------------------------------------------------


def write_trace(directory, *, runs: int = 2) -> None:
    tracer = TraceLogger(directory, enabled=True)
    for index in range(runs):
        tracer.start_run(user_id=f"u{index}", session_id="s1", mode="react")
        tracer.log("llm_call", latency_ms=900 + index)
        tracer.log("tool_call", tool="get_weather")
        if index == 0:
            tracer.end_run(status="success", latency_ms=1200, steps=2, tool_calls=["get_weather"])
        else:
            tracer.log("run_budget_exceeded", spent_ms=60001, budget_ms=60000, stage="llm_call")
            tracer.end_run(status="budget_exceeded", latency_ms=61000, steps=3)


def test_trace_stats_aggregates_runs_and_statuses(tmp_path, capsys):
    write_trace(tmp_path)

    assert main(["trace", "stats", "--dir", str(tmp_path)]) == 0

    out = capsys.readouterr().out
    assert "2 个 run" in out
    assert "success" in out and "budget_exceeded" in out
    assert "预算熔断：1" in out


def test_trace_tail_prints_recent_events(tmp_path, capsys):
    write_trace(tmp_path)

    assert main(["trace", "tail", "--dir", str(tmp_path), "--n", "2"]) == 0

    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert len(lines) == 2
    # 倒序：最后写入的 run_end 在最前
    assert "run_end" in lines[0]


def test_trace_stats_json_output(tmp_path, capsys):
    write_trace(tmp_path)

    assert main(["trace", "stats", "--dir", str(tmp_path), "--json"]) == 0

    stats = json.loads(capsys.readouterr().out)
    assert stats["runs"] == 2
    assert stats["statuses"] == {"budget_exceeded": 1, "success": 1}
    assert stats["avg_latency_ms"] == 31100
    assert stats["budget_exceeded"] == 1


def test_trace_without_records_reports_and_exits_zero(tmp_path, capsys):
    assert main(["trace", "stats", "--dir", str(tmp_path)]) == 0
    assert "没有找到 Trace 记录" in capsys.readouterr().err


def test_read_trace_events_skips_malformed_lines(tmp_path):
    (tmp_path / "2026-10-10.jsonl").write_text(
        '{"kind": "run_end", "run_id": "r1", "status": "success"}\n'
        "{ 这不是 JSON\n"
        '{"kind": "tool_call", "run_id": "r1"}\n',
        encoding="utf-8",
    )

    read = inspect_mod.read_trace_events(tmp_path, limit=10)

    assert len(read.events) == 2
    assert read.malformed == 1
    assert read.events[0]["kind"] == "tool_call"  # 倒序读取


def test_read_trace_events_respects_limit(tmp_path):
    lines = "\n".join(json.dumps({"kind": "tool_call", "n": index}) for index in range(10))
    (tmp_path / "2026-10-10.jsonl").write_text(lines + "\n", encoding="utf-8")

    read = inspect_mod.read_trace_events(tmp_path, limit=3)

    assert len(read.events) == 3
    assert read.events[0]["n"] == 9  # 最新的优先


def test_read_trace_events_rejects_non_positive_limit(tmp_path):
    with pytest.raises(ValueError, match="limit"):
        inspect_mod.read_trace_events(tmp_path, limit=0)


def test_trace_stats_on_empty_input():
    stats = inspect_mod.trace_stats([])

    assert stats["runs"] == 0
    assert stats["avg_latency_ms"] is None
    assert stats["by_kind"] == {}
