"""Trace 记录与脱敏测试（design.md 15）。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from first_agent.core.tracing import (
    TraceLogger,
    hash_identifier,
    mask_sensitive,
    mask_text,
    sanitize,
)


def test_mask_text_hides_phone_and_email():
    masked = mask_text("联系 13812345678 或 a.b@example.com")
    assert "13812345678" not in masked
    assert "138****5678" in masked
    assert "a.b@example.com" not in masked
    assert "a***@example.com" in masked


def test_mask_sensitive_keeps_only_first_char():
    assert mask_sensitive("张三丰") == "张***"
    assert mask_sensitive("李") == "李*"
    assert mask_sensitive("") == ""


def test_sanitize_hashes_ids_and_masks_sensitive_values():
    payload = {
        "user_id": "u-123",
        "session_id": "s-456",
        "memory": {"user_name": "张三"},
        "note": "电话 13812345678",
    }

    clean = sanitize(payload)

    assert clean["user_id"] == hash_identifier("u-123")
    assert clean["session_id"] == hash_identifier("s-456")
    assert clean["memory"]["user_name"] == "张***"
    assert "13812345678" not in clean["note"]


def test_sanitize_full_mode_is_passthrough():
    payload = {"user_id": "u-123", "note": "电话 13812345678"}
    assert sanitize(payload, mode="full") == payload


def test_sanitize_rejects_unknown_mode():
    with pytest.raises(ValueError):
        sanitize({}, mode="nope")


def test_trace_logger_writes_sanitized_jsonl(tmp_path):
    logger = TraceLogger(tmp_path, mode="safe")
    run_id = logger.start_run(user_id="u-123", session_id="s-456", mode="react")
    logger.log("llm_call", latency_ms=12)
    logger.end_run(status="success", latency_ms=34, final_answer="好的")

    files = list(Path(tmp_path).glob("*.jsonl"))
    assert len(files) == 1

    records = [
        json.loads(line) for line in files[0].read_text(encoding="utf-8").splitlines()
    ]
    assert [record["kind"] for record in records] == ["run_start", "llm_call", "run_end"]
    assert all(record["run_id"] == run_id for record in records)
    assert records[0]["user_id"] == hash_identifier("u-123")
    assert records[-1]["status"] == "success"
    assert records[-1]["latency_ms"] == 34


def test_trace_logger_disabled_is_noop(tmp_path):
    logger = TraceLogger(tmp_path, enabled=False)
    logger.start_run(user_id="u", session_id="s")
    logger.log("llm_call")
    logger.end_run(status="success")

    assert list(Path(tmp_path).glob("*.jsonl")) == []


def test_trace_logger_rejects_unknown_mode(tmp_path):
    with pytest.raises(ValueError):
        TraceLogger(tmp_path, mode="nope")
