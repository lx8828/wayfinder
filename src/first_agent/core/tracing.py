"""Trace 记录与隐私脱敏。对应 design.md 第 15 节。

- 每天一个文件：``var/traces/YYYY-MM-DD.jsonl``
- ``TRACE_MODE=safe``（默认）：user_id / session_id 做 hash；手机号、邮箱做掩码；
  敏感 key（如 ``user_name``）整体掩码
- ``TRACE_MODE=full``：仅本地调试使用，不脱敏
- ``enabled=False``：完全 no-op（测试用）
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import uuid
from datetime import date, datetime
from pathlib import Path
from typing import Any

TRACE_MODES = ("safe", "full")

# 值需要整体掩码的 key
SENSITIVE_KEYS = frozenset({"user_name", "姓名", "name", "phone", "email"})
# 值需要 hash 的 key
_ID_KEYS = ("user_id", "session_id")

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")


def hash_identifier(value: str, *, salt: str = "") -> str:
    """把标识符映射为定长摘要，便于关联同一次 run 又不泄露原值。"""

    return hashlib.sha256(f"{salt}{value}".encode("utf-8")).hexdigest()[:16]


def mask_text(text: str) -> str:
    """掩码文本中的手机号与邮箱。"""

    text = _PHONE_RE.sub(lambda m: f"{m.group(0)[:3]}****{m.group(0)[-4:]}", text)
    text = _EMAIL_RE.sub(
        lambda m: f"{m.group(0)[0]}***@{m.group(0).split('@', 1)[1]}", text
    )
    return text


def mask_sensitive(value: str) -> str:
    """对姓名一类的敏感值整体掩码：只保留首字符。"""

    if not value:
        return value
    return f"{value[0]}*" if len(value) == 1 else f"{value[0]}{'*' * 3}"


def sanitize(
    value: Any,
    *,
    mode: str = "safe",
    salt: str = "",
    key: str | None = None,
) -> Any:
    """递归脱敏。``full`` 模式原样返回。"""

    if mode not in TRACE_MODES:
        raise ValueError(f"未知 TRACE_MODE：{mode!r}")
    if mode == "full":
        return value
    if isinstance(value, dict):
        return {k: sanitize(v, mode=mode, salt=salt, key=k) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize(item, mode=mode, salt=salt, key=key) for item in value]
    if isinstance(value, str):
        if key and any(id_key in key for id_key in _ID_KEYS):
            return hash_identifier(value, salt=salt)
        if key in SENSITIVE_KEYS:
            return mask_sensitive(value)
        return mask_text(value)
    return value


class TraceLogger:
    """把一次 run 的事件写成 JSONL；线程安全。"""

    def __init__(
        self,
        directory: str | Path = "var/traces",
        *,
        mode: str = "safe",
        enabled: bool = True,
        salt: str = "",
    ) -> None:
        if mode not in TRACE_MODES:
            raise ValueError(f"未知 TRACE_MODE：{mode!r}")
        self.directory = Path(directory)
        self.mode = mode
        self.enabled = enabled
        self.salt = salt
        self.run_id: str | None = None
        self._lock = threading.Lock()

    def start_run(
        self,
        *,
        user_id: str,
        session_id: str,
        mode: str = "react",
        mode_selection: str | None = None,
    ) -> str:
        """开启一次 run 并返回 run_id。``mode_selection`` 记录用户手动选择的模式。"""

        self.run_id = f"run_{uuid.uuid4().hex[:12]}"
        self.log(
            "run_start",
            user_id=user_id,
            session_id=session_id,
            mode=mode,
            mode_selection=mode_selection or mode,
        )
        return self.run_id

    def log(self, kind: str, **payload: Any) -> None:
        if not self.enabled:
            return
        record: dict[str, Any] = {
            "kind": kind,
            "run_id": self.run_id,
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        record.update(payload)
        record = sanitize(record, mode=self.mode, salt=self.salt)
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            path = self.directory / f"{date.today().isoformat()}.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def end_run(
        self,
        *,
        status: str,
        latency_ms: int | None = None,
        final_answer: str | None = None,
        **extra: Any,
    ) -> None:
        payload: dict[str, Any] = {"status": status, **extra}
        if latency_ms is not None:
            payload["latency_ms"] = latency_ms
        if final_answer is not None:
            payload["final_answer"] = final_answer
        self.log("run_end", **payload)
