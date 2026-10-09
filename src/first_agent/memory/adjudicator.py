"""记忆关系裁决（LLM）。对应 design.md 12.3 的 Adjudicator 职责。

**只在 key 已存在（有旧值）时被调用**——无旧值时不需要判断关系，直接 add。
只输出「新值与旧值的关系」，**不输出** `add/update/keep`。

失败策略（design 13「Adjudicator 失败：保留旧记忆」）：返回 ``unsupported``，
由 ``decide_action`` 映射为 ``skip``。这样既不误改旧值，也不假装两者等价
（不refresh `last_seen_at`），且**不会丢数据**。
"""

from __future__ import annotations

from typing import Any

from first_agent.core.client import JSONValidationError, LLMClient
from first_agent.schemas.memory import MemoryRelation

SYSTEM_PROMPT = """你是一个记忆裁决器。判断**新值**与**同一个 key 的旧值**之间的关系。

- same：表达同一件事，语义等价（如“重庆” 与 “重庆市”）
- conflict：互相冲突，新值应当取代旧值（如“重庆” 与 “杭州”）
- complementary：不冲突，但旧值的结构装不下新信息（如“不吃辣” 与 “喜欢甜食”）
- unsupported：无法用单值承载（如旧值“不吃辣”、新值“喜欢川菜并且怕冷”）

只返回 JSON：{"relation": "conflict", "reason": "城市变更"}
"""

FALLBACK = MemoryRelation(relation="unsupported", reason="裁决失败，保守跳过（保留旧记忆）")


class MemoryAdjudicator:
    def __init__(self, client: LLMClient, *, tracer: Any = None) -> None:
        self.client = client
        self.tracer = tracer

    def adjudicate(self, *, key: str, old_value: str, new_value: str) -> MemoryRelation:
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"key: {key}\n"
                    f"旧值: {old_value}\n"
                    f"新值: {new_value}\n"
                ),
            },
        ]
        try:
            relation = self.client.chat_json(messages, MemoryRelation)
        except JSONValidationError as exc:
            self._trace("memory_adjudicate_failed", key=key, error=str(exc))
            return FALLBACK
        self._trace("memory_adjudicated", key=key, relation=relation.relation)
        return relation

    def _trace(self, kind: str, **payload: Any) -> None:
        if self.tracer is not None:
            self.tracer.log(kind, **payload)
