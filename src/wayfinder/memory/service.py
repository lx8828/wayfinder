"""记忆服务：抽取 → 裁决 → 落库。对应 design.md 12.3 的 MemoryService 职责。

流水线（严格单向，三层不串味）：

    Extractor 判「值不值得存」
      → 有旧值才调 Adjudicator 判「什么关系」
        → 代码按 relation 映射动作（add/keep/update/skip）
          → MemoryStore 落库

**LLM 永远不会输出数据库动作**，因此 v1「LLM 说 add 但 service 不处理 → 丢数据」
那一类缺陷在结构上不可能再出现。``add`` 只在 key 不存在时由代码产生。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from wayfinder.core.client import LLMClient
from wayfinder.memory.adjudicator import MemoryAdjudicator
from wayfinder.memory.extractor import MemoryExtractor
from wayfinder.memory.store import MemoryStore


@dataclass(frozen=True)
class MemoryAction:
    key: str
    action: str  # add / keep / update / skip
    relation: str | None = None
    reason: str = ""


@dataclass
class RememberOutcome:
    actions: list[MemoryAction] = field(default_factory=list)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    extraction_failed: bool = False

    @property
    def actions_by_key(self) -> dict[str, str]:
        """``{key: action}``，供评测的 ``expected_memory_delta`` 对比。"""

        return {item.key: item.action for item in self.actions}

    def action_for(self, key: str) -> str | None:
        for item in self.actions:
            if item.key == key:
                return item.action
        return None


class MemoryService:
    def __init__(
        self,
        client: LLMClient,
        store: MemoryStore,
        *,
        extractor: MemoryExtractor | None = None,
        adjudicator: MemoryAdjudicator | None = None,
        tracer: Any = None,
    ) -> None:
        self.store = store
        self.tracer = tracer
        self.extractor = extractor or MemoryExtractor(client, tracer=tracer)
        self.adjudicator = adjudicator or MemoryAdjudicator(client, tracer=tracer)

    def remember(self, user_input: str, *, user_id: str) -> RememberOutcome:
        extraction = self.extractor.extract(user_input, user_id=user_id)
        outcome = RememberOutcome(
            dropped=list(extraction.dropped), extraction_failed=extraction.failed
        )

        for candidate in extraction.candidates:
            old = self.store.get(candidate.user_id, candidate.key)
            relation = None
            reason = ""

            if old is not None:
                judged = self.adjudicator.adjudicate(
                    key=candidate.key, old_value=old.value, new_value=candidate.value
                )
                relation, reason = judged.relation, judged.reason

            action = self.store.apply_candidate(candidate, relation)
            outcome.actions.append(
                MemoryAction(key=candidate.key, action=action, relation=relation, reason=reason)
            )
            self._trace(
                "memory_action",
                key=candidate.key,
                action=action,
                relation=relation,
                old_value=old.value if old else None,
                new_value=candidate.value,
            )

        return outcome

    def _trace(self, kind: str, **payload: Any) -> None:
        if self.tracer is not None:
            self.tracer.log(kind, **payload)
