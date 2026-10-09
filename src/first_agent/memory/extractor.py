"""长期记忆抽取（LLM）。对应 design.md 12.3 的 Extractor 职责。

- 只判断「值不值得长期保存」，输出 `MemoryCandidate` 列表或空列表
- **不输出数据库动作**（`add/update/keep` 由 MemoryService 确定性裁决）

健壮性设计：LLM 输出先用**宽松结构**接收，再逐条规范化（key 别名 / category 别名），
最后逐条构造候选。**单条非法不会毁掉整批**：非法项进入 ``dropped`` 并写 Trace。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from first_agent.core.client import JSONValidationError, LLMClient
from first_agent.schemas.memory import CATEGORIES, MemoryCandidate

# v2.0 支持的规范 key（design.md 12.2）
# 注：`dietary_preference` 是 S5 用**真实 v1 数据**验证映射时补上的——
# 原始设计只列了 5 个 key，漏掉了 v1 实际存在的「饮食偏好」，
# 若不补，用户说「我不吃辣」会在抽取阶段被静默丢弃。
CANONICAL_KEYS: tuple[str, ...] = (
    "home_city",
    "timezone",
    "transport_mode",
    "user_name",
    "dietary_preference",
    "long_term_plan",
)

# 别名 → 规范 key（含 v1 的中文 key，见 design.md 18.2 映射表）
KEY_ALIASES: dict[str, str] = {
    "居住地": "home_city",
    "用户城市": "home_city",
    "城市": "home_city",
    "home": "home_city",
    "时区": "timezone",
    "出行偏好": "transport_mode",
    "交通方式": "transport_mode",
    "通勤方式": "transport_mode",
    "用户姓名": "user_name",
    "姓名": "user_name",
    "名字": "user_name",
    "name": "user_name",
    "饮食偏好": "dietary_preference",
    "口味": "dietary_preference",
    "忌口": "dietary_preference",
    "长期计划": "long_term_plan",
    "计划": "long_term_plan",
}

CATEGORY_ALIASES: dict[str, str] = {
    "用户身份": "user_identity",
    "身份": "user_identity",
    "用户偏好": "preference",
    "偏好": "preference",
    "喜好": "preference",
    "习惯": "habit",
    "长期计划": "long_term_plan",
}

DEFAULT_CATEGORY = "preference"

SYSTEM_PROMPT = """你是一个记忆提取器。
任务：从用户输入中提取**值得长期保存**的信息，每条单独列出。

值得保存：姓名、长期居住地、时区、出行偏好、饮食偏好、习惯、长期计划。
不要保存：一次性事件、临时天气、当前任务过程、普通闲聊。

key 只能使用以下规范名称之一：
- home_city（居住地 / 常驻城市）
- timezone（时区）
- transport_mode（出行偏好 / 常用交通方式）
- user_name（姓名）
- dietary_preference（饮食偏好 / 忌口）
- long_term_plan（长期计划）

category 只能是：user_identity / preference / habit / long_term_plan

只返回 JSON：
{"memories": [{"key": "home_city", "value": "重庆", "category": "user_identity"}]}

没有值得保存的信息时：
{"memories": []}
"""


class RawMemory(BaseModel):
    """宽松接收：key/value/category 都按字符串接，再逐条规范化。"""

    key: str = ""
    value: str = ""
    category: str = ""


class MemoryExtraction(BaseModel):
    memories: list[RawMemory] = Field(default_factory=list)


@dataclass
class ExtractionOutcome:
    candidates: list[MemoryCandidate] = field(default_factory=list)
    dropped: list[dict[str, Any]] = field(default_factory=list)
    failed: bool = False

    @property
    def keys(self) -> list[str]:
        return [candidate.key for candidate in self.candidates]


def normalize_key(key: str) -> str | None:
    """规范化记忆 key；无法识别时返回 ``None``。"""

    cleaned = (key or "").strip()
    if not cleaned:
        return None
    lowered = cleaned.lower()
    if lowered in CANONICAL_KEYS:
        return lowered
    return KEY_ALIASES.get(cleaned) or KEY_ALIASES.get(lowered)


def normalize_category(category: str) -> str:
    """规范化 category；无法识别时回落到 ``preference``。"""

    cleaned = (category or "").strip()
    if cleaned in CATEGORIES:
        return cleaned
    return CATEGORY_ALIASES.get(cleaned, DEFAULT_CATEGORY)


class MemoryExtractor:
    def __init__(
        self,
        client: LLMClient,
        *,
        tracer: Any = None,
        max_input_chars: int = 2000,
    ) -> None:
        self.client = client
        self.tracer = tracer
        self.max_input_chars = max_input_chars

    def extract(self, user_input: str, *, user_id: str) -> ExtractionOutcome:
        text = (user_input or "").strip()
        if not text:
            return ExtractionOutcome()

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": text[: self.max_input_chars]},
        ]
        try:
            extraction = self.client.chat_json(messages, MemoryExtraction)
        except JSONValidationError as exc:
            # design 13：Memory 失败 → no-op，绝不污染数据库
            self._trace("memory_extract_failed", error=str(exc))
            return ExtractionOutcome(failed=True)

        return self._normalize(extraction, user_id=user_id)

    # -- 内部 -------------------------------------------------------------
    def _normalize(self, extraction: MemoryExtraction, *, user_id: str) -> ExtractionOutcome:
        outcome = ExtractionOutcome()
        built: list[MemoryCandidate] = []

        for raw in extraction.memories:
            key = normalize_key(raw.key)
            value = (raw.value or "").strip()

            if key is None:
                outcome.dropped.append({"reason": "unknown_key", "key": raw.key, "value": value})
                continue
            if not value:
                outcome.dropped.append({"reason": "empty_value", "key": key})
                continue

            try:
                built.append(
                    MemoryCandidate(
                        user_id=user_id,
                        key=key,
                        value=value,
                        category=normalize_category(raw.category),
                    )
                )
            except Exception as exc:  # noqa: BLE001 - 单条非法不应毁掉整批
                outcome.dropped.append({"reason": "invalid", "key": key, "error": str(exc)})

        # 同批内同 key 去重：保留**最后**一次表述（顺序保留首次出现的位置）
        deduped: dict[str, MemoryCandidate] = {}
        for candidate in built:
            if candidate.key in deduped:
                outcome.dropped.append({"reason": "duplicate_key", "key": candidate.key})
            deduped[candidate.key] = candidate

        outcome.candidates = list(deduped.values())
        if outcome.dropped:
            self._trace("memory_extract_dropped", items=outcome.dropped)
        return outcome

    def _trace(self, kind: str, **payload: Any) -> None:
        if self.tracer is not None:
            self.tracer.log(kind, **payload)
