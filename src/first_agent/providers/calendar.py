"""本地日程 provider。对应 design.md 11.5 的 ``get_calendar_events``。

从本地 JSON 读取：``{"<user_id>": {"YYYY-MM-DD": ["事件", ...]}}``。
文件不存在或用户无记录 → 返回空日程（不是错误）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from first_agent.providers.base import ProviderUnavailable

DEFAULT_PATH = "var/calendar.json"


class LocalCalendarProvider:
    def __init__(self, path: str | Path = DEFAULT_PATH, *, clock: Any = None) -> None:
        self.path = Path(path)

    def get_events(self, user_id: str, date: str) -> dict[str, Any]:
        calendar = self._load()
        events = calendar.get(user_id, {}).get(date, [])
        if not isinstance(events, list):
            events = [str(events)]
        return {"date": date, "events": [str(item) for item in events]}

    def _load(self) -> dict[str, dict[str, list[str]]]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ProviderUnavailable(f"日程文件无法读取：{exc}") from exc
        if not isinstance(payload, dict):
            raise ProviderUnavailable(f"日程文件格式应为 JSON 对象：{self.path}")
        return payload
