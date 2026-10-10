"""只读运维视图：``memory`` / ``trace`` 子命令的数据层（design.md 15）。

放在 ``core`` 的理由：这里没有业务逻辑，只有「把已有数据读出来给人看」，
且必须可被单元测试直接调用——CLI 只负责打印。

Trace 目录约定：``var/traces/YYYY-MM-DD.jsonl``，每天一个文件，**新的在后**。
读取时从最新文件往前取，凑够 ``limit`` 条即停（不必扫全量历史）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from wayfinder.memory.store import MemoryRecord

STATUS_KINDS = ("run_end",)


def list_memories(store: Any, user_id: str | None = None) -> dict[str, list[MemoryRecord]]:
    """``user_id → 记忆列表``；``user_id`` 为 None 时返回全部用户。"""

    users = [user_id] if user_id else store.users()
    return {user: list(store.all(user).values()) for user in users}


def format_memories(by_user: dict[str, list[MemoryRecord]]) -> str:
    if not by_user:
        return "（没有长期记忆）"
    lines: list[str] = []
    for user, records in by_user.items():
        lines.append(f"用户 {user}（{len(records)} 条）")
        if not records:
            lines.append("  （空）")
            continue
        lines.append("  key                  value                      category          last_seen_at")
        for record in records:
            lines.append(
                f"  {record.key:<20} {record.value:<26} {record.category:<17} "
                f"{record.last_seen_at}"
            )
    return "\n".join(lines)


def memory_as_json(by_user: dict[str, list[MemoryRecord]]) -> str:
    payload = {
        user: [
            {
                "key": record.key,
                "value": record.value,
                "category": record.category,
                "source": record.source,
                "updated_at": record.updated_at,
                "last_seen_at": record.last_seen_at,
            }
            for record in records
        ]
        for user, records in by_user.items()
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


# -- Trace -----------------------------------------------------------------


@dataclass
class TraceRead:
    events: list[dict[str, Any]] = field(default_factory=list)
    malformed: int = 0
    files: int = 0


def read_trace_events(directory: str | Path, *, limit: int = 50) -> TraceRead:
    """按时间倒序读取最近 ``limit`` 条事件（从最新的文件往前读）。"""

    if limit <= 0:
        raise ValueError("limit 必须为正数")
    folder = Path(directory)
    if not folder.exists():
        return TraceRead()

    files = sorted(folder.glob("*.jsonl"), reverse=True)
    read = TraceRead(files=len(files))
    for path in files:
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            if not line.strip():
                continue
            try:
                read.events.append(json.loads(line))
            except json.JSONDecodeError:
                read.malformed += 1
                continue
            if len(read.events) >= limit:
                return read
    return read


def trace_stats(events: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """按事件聚合出运维关心的数字（run 数、状态分布、耗时、降级与熔断次数）。"""

    runs = {event.get("run_id") for event in events if event.get("run_id")}
    by_kind: dict[str, int] = {}
    statuses: dict[str, int] = {}
    latencies: list[int] = []
    steps: list[int] = []
    tool_calls = 0
    for event in events:
        kind = str(event.get("kind", "unknown"))
        by_kind[kind] = by_kind.get(kind, 0) + 1
        if kind in STATUS_KINDS:
            status = str(event.get("status", "unknown"))
            statuses[status] = statuses.get(status, 0) + 1
        latency = event.get("latency_ms")
        if kind in STATUS_KINDS and isinstance(latency, (int, float)):
            latencies.append(int(latency))
        step_count = event.get("steps")
        if kind in STATUS_KINDS and isinstance(step_count, int):
            steps.append(step_count)
        if kind in ("llm_call", "tool_call"):
            tool_calls += 1

    return {
        "events": len(events),
        "runs": len(runs),
        "by_kind": dict(sorted(by_kind.items())),
        "statuses": dict(sorted(statuses.items())),
        "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
        "max_latency_ms": max(latencies) if latencies else None,
        "avg_steps": round(sum(steps) / len(steps), 2) if steps else None,
        "budget_exceeded": by_kind.get("run_budget_exceeded", 0),
        "degraded": by_kind.get("plan_degraded_to_react", 0),
        "tool_errors": by_kind.get("tool_error", 0),
        "tool_calls": tool_calls,
    }


def format_events(events: Iterable[dict[str, Any]], *, limit: int = 50) -> str:
    lines: list[str] = []
    for event in list(events)[:limit]:
        kind = event.get("kind", "unknown")
        run_id = event.get("run_id") or "—"
        ts = event.get("ts", "—")
        rest = {
            key: value
            for key, value in event.items()
            if key not in ("kind", "run_id", "ts")
        }
        lines.append(f"{ts}  {kind:<24} {run_id}  {json.dumps(rest, ensure_ascii=False)}")
    return "\n".join(lines) if lines else "（没有 Trace 记录）"


def format_stats(stats: dict[str, Any]) -> str:
    lines = [
        f"事件数：{stats['events']}（{stats['runs']} 个 run）",
        f"状态分布：{stats['statuses'] or '（无 run_end）'}",
        f"平均耗时：{stats['avg_latency_ms']} ms　最慢：{stats['max_latency_ms']} ms",
        f"平均步数：{stats['avg_steps']}",
        f"工具调用：{stats['tool_calls']}　工具错误：{stats['tool_errors']}",
        f"预算熔断：{stats['budget_exceeded']}　Plan 降级到 ReAct：{stats['degraded']}",
        "",
        "事件类型分布：",
    ]
    lines.extend(f"  {kind:<28} {count}" for kind, count in stats["by_kind"].items())
    return "\n".join(lines)
