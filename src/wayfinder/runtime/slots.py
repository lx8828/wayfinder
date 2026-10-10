"""本轮槽位抽取（LLM）。对应 design.md 8.2「提取本轮槽位」与 8.3「多轮追问」。

抽取的是**用户原话**（如 ``date="明天下午"``），**不换算**成具体日期——换算留给
``resolve_datetime`` 工具，这样 Plan 里能看到「先解析时间、再查天气」的依赖链。

抽取失败不阻塞：返回空 patch，交由 Planner 的 ``need_info`` 兜底追问。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from wayfinder.core.client import JSONValidationError, LLMClient
from wayfinder.schemas.session import SLOT_FIELDS

SYSTEM_PROMPT = """你负责从用户这句话里抽取出行任务槽位。

字段（用户没提到的填 null，**不要猜测**）：
- destination：目的地
- origin：出发地
- date：日期。保留用户原话（如“明天下午”“下周三”），**不要**换算成具体日期
- purpose：出行目的
- transport_mode：出行方式（步行/地铁/开车…）

只返回 JSON，不要解释：
{"destination": null, "origin": null, "date": "明天下午", "purpose": "看电影", "transport_mode": null}
"""


class SlotPatch(BaseModel):
    destination: str | None = None
    origin: str | None = None
    date: str | None = None
    purpose: str | None = None
    transport_mode: str | None = None

    def non_empty(self) -> dict[str, str]:
        return {name: value for name in SLOT_FIELDS if (value := getattr(self, name))}


class SlotExtractor:
    def __init__(self, client: LLMClient, *, tracer: Any = None) -> None:
        self.client = client
        self.tracer = tracer

    def extract(self, user_input: str) -> SlotPatch:
        text = (user_input or "").strip()
        if not text:
            return SlotPatch()

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ]
        try:
            return self.client.chat_json(messages, SlotPatch)
        except JSONValidationError as exc:
            self._trace("slot_extract_failed", error=str(exc))
            return SlotPatch()

    def _trace(self, kind: str, **payload: Any) -> None:
        if self.tracer is not None:
            self.tracer.log(kind, **payload)
