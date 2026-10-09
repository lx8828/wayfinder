"""记忆服务测试：三层职责分离 + **数据永不丢失**（design.md 12.3 / 13）。

这是风险最高的一块：v1 的缺陷正是「LLM 说 add，但 service 不处理 → 静默丢数据」。
v2 把动作裁决改成确定性代码后，这里用显式不变量把它钉死：

1. 行数**单调不减**（记忆只会新增或覆盖，不会被删掉）
2. 任何 key 的 value **永不为空**
3. 无论 LLM 给出哪种 relation，**已存在的 key 操作后依然存在**
"""

from __future__ import annotations

import pytest

from first_agent.core.client import LLMClient
from first_agent.core.db import Database
from first_agent.evals.scripted import ScriptedLLM, json_obj
from first_agent.memory.extractor import ExtractionOutcome
from first_agent.memory.service import MemoryService
from first_agent.memory.store import MemoryStore
from first_agent.schemas.memory import MemoryCandidate, MemoryRelation

USER = "u1"


class StubExtractor:
    """按预设返回候选，不调用 LLM。"""

    def __init__(self, candidates=(), *, dropped=None, failed=False) -> None:
        self._candidates = list(candidates)
        self._dropped = list(dropped or [])
        self._failed = failed
        self.calls: list[str] = []

    def extract(self, user_input, *, user_id) -> ExtractionOutcome:
        self.calls.append(user_input)
        return ExtractionOutcome(
            candidates=[item.model_copy(update={"user_id": user_id}) for item in self._candidates],
            dropped=list(self._dropped),
            failed=self._failed,
        )


class StubAdjudicator:
    def __init__(self, relation: str = "conflict", *, reason: str = "stub") -> None:
        self.relation = relation
        self.reason = reason
        self.calls: list[tuple[str, str, str]] = []

    def adjudicate(self, *, key, old_value, new_value) -> MemoryRelation:
        self.calls.append((key, old_value, new_value))
        return MemoryRelation(relation=self.relation, reason=self.reason)


def candidate(key: str = "home_city", value: str = "重庆", category: str = "user_identity"):
    return MemoryCandidate(user_id=USER, key=key, value=value, category=category)


def build(candidates=(), *, relation="conflict", dropped=None, failed=False, seed=()):
    store = MemoryStore(Database(":memory:"))
    for key, value in seed:
        store.apply_candidate(
            MemoryCandidate(user_id=USER, key=key, value=value, category="user_identity")
        )
    extractor = StubExtractor(candidates, dropped=dropped, failed=failed)
    adjudicator = StubAdjudicator(relation)
    service = MemoryService(None, store, extractor=extractor, adjudicator=adjudicator)
    return service, store, extractor, adjudicator


def invariants(store) -> dict[str, str]:
    """每次操作后都必须成立：没有空值。"""

    values = store.values(USER)
    assert all(value.strip() for value in values.values()), "出现了空记忆值"
    return values


# -- 无旧值：直接 add，不调 Adjudicator -------------------------------------


def test_first_write_is_add_and_skips_adjudicator():
    service, store, _, adjudicator = build([candidate()])

    outcome = service.remember("我住在重庆", user_id=USER)

    assert outcome.action_for("home_city") == "add"
    assert adjudicator.calls == []  # 无旧值不该浪费一次 LLM 调用
    assert store.values(USER) == {"home_city": "重庆"}


def test_adjudicator_not_called_for_new_key_when_other_keys_exist():
    """「库里有别的记忆」不等于「这个 key 有旧值」。"""

    service, _, _, adjudicator = build(
        [candidate(key="timezone", value="Asia/Shanghai", category="preference")],
        seed=[("home_city", "重庆")],
    )

    service.remember("我用东八区", user_id=USER)

    assert adjudicator.calls == []


# -- 有旧值：调 Adjudicator，动作由 relation 决定 ---------------------------


def test_existing_key_invokes_adjudicator_with_old_and_new():
    service, _, _, adjudicator = build([candidate(value="杭州")], seed=[("home_city", "重庆")])

    service.remember("我搬到杭州了", user_id=USER)

    assert adjudicator.calls == [("home_city", "重庆", "杭州")]


@pytest.mark.parametrize(
    ("relation", "expected"),
    [
        ("same", "keep"),
        ("conflict", "update"),
        ("complementary", "skip"),
        ("unsupported", "skip"),
    ],
)
def test_relation_maps_to_deterministic_action(relation, expected):
    service, store, _, _ = build([candidate(value="杭州")], relation=relation, seed=[("home_city", "重庆")])

    outcome = service.remember("我搬到杭州了", user_id=USER)

    assert outcome.action_for("home_city") == expected
    invariants(store)


def test_keep_keeps_old_value():
    service, store, _, _ = build([candidate(value="重庆市")], relation="same", seed=[("home_city", "重庆")])

    service.remember("我住重庆", user_id=USER)

    assert store.values(USER)["home_city"] == "重庆"


def test_update_replaces_value():
    service, store, _, _ = build([candidate(value="杭州")], relation="conflict", seed=[("home_city", "重庆")])

    service.remember("我搬到杭州了", user_id=USER)

    assert store.values(USER) == {"home_city": "杭州"}
    assert store.get(USER, "home_city").category == "user_identity"


def test_skip_leaves_old_value_completely_untouched():
    service, store, _, _ = build(
        [candidate(value="喜欢甜食", category="preference")],
        relation="complementary",
        seed=[("home_city", "重庆")],
    )

    service.remember("我还喜欢甜食", user_id=USER)

    assert store.values(USER) == {"home_city": "重庆"}


def test_adjudicator_failure_preserves_old_value():
    """真实 Adjudicator 失败时返回 unsupported；旧值必须原封不动。"""

    service, store, _, _ = build([candidate(value="杭州")], relation="unsupported", seed=[("home_city", "重庆")])

    outcome = service.remember("我搬到杭州了", user_id=USER)

    assert outcome.action_for("home_city") == "skip"
    assert store.values(USER) == {"home_city": "重庆"}


# -- 数据永不丢失（v1 缺陷的回归护栏）--------------------------------------


@pytest.mark.parametrize("relation", ["same", "conflict", "complementary", "unsupported"])
def test_existing_key_survives_every_relation(relation):
    service, store, _, _ = build([candidate(value="杭州")], relation=relation, seed=[("home_city", "重庆")])

    service.remember("我搬到杭州了", user_id=USER)

    values = invariants(store)
    assert "home_city" in values  # 任何 relation 都不会把 key 弄丢
    assert values["home_city"]


def test_sequence_never_loses_rows_or_values():
    store = MemoryStore(Database(":memory:"))
    row_counts: list[int] = []

    def remember(candidates, relation):
        service = MemoryService(
            None, store, extractor=StubExtractor(candidates), adjudicator=StubAdjudicator(relation)
        )
        service.remember("（测试输入）", user_id=USER)
        row_counts.append(len(store.all(USER)))
        invariants(store)

    remember([candidate(), candidate(key="user_name", value="张三")], "same")
    remember([candidate(key="user_name", value="张三")], "same")
    remember([candidate(value="杭州"), candidate(key="user_name", value="张三")], "conflict")
    remember([candidate(value="杭州")], "complementary")
    remember([candidate(value="成都")], "unsupported")

    assert row_counts == sorted(row_counts)  # 单调不减
    assert store.values(USER) == {"home_city": "杭州", "user_name": "张三"}


def test_extraction_failure_writes_nothing():
    service, store, _, adjudicator = build(failed=True)

    outcome = service.remember("我叫张三", user_id=USER)

    assert outcome.extraction_failed is True
    assert store.values(USER) == {}
    assert adjudicator.calls == []


def test_zero_candidates_writes_nothing():
    service, store, _, _ = build([])

    outcome = service.remember("今天天气不错", user_id=USER)

    assert outcome.actions == []
    assert store.values(USER) == {}


# -- 结果可观测 ------------------------------------------------------------


def test_outcome_exposes_actions_and_dropped():
    service, _, _, _ = build(
        [candidate(), candidate(key="user_name", value="张三")],
        dropped=[{"reason": "unknown_key", "key": "favorite_color"}],
    )

    outcome = service.remember("我叫张三，住在重庆", user_id=USER)

    assert outcome.actions_by_key == {"home_city": "add", "user_name": "add"}
    assert outcome.dropped[0]["key"] == "favorite_color"


def test_tracer_receives_memory_action_events():
    class Recorder:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict]] = []

        def log(self, kind, **payload):
            self.events.append((kind, payload))

    recorder = Recorder()
    store = MemoryStore(Database(":memory:"))
    service = MemoryService(
        None,
        store,
        extractor=StubExtractor([candidate()]),
        adjudicator=StubAdjudicator("same"),
        tracer=recorder,
    )

    service.remember("我住在重庆", user_id=USER)

    kind, payload = recorder.events[-1]
    assert kind == "memory_action"
    assert payload["action"] == "add"
    assert payload["new_value"] == "重庆"


# -- 用真实 Extractor + Adjudicator 跑一遍（校验调用顺序）------------------


def test_end_to_end_with_real_extractor_and_adjudicator():
    store = MemoryStore(Database(":memory:"))
    sdk = ScriptedLLM(
        [
            # 1) 第一次抽取：新增
            json_obj(
                {"memories": [{"key": "home_city", "value": "重庆", "category": "user_identity"}]}
            ),
            # 2) 第二次抽取（中文 key，需规范化）→ 3) 裁决为 same
            json_obj(
                {"memories": [{"key": "居住地", "value": "重庆", "category": "用户身份"}]}
            ),
            json_obj({"relation": "same", "reason": "同一城市"}),
            # 4) 第三次抽取 → 5) 裁决为 conflict
            json_obj(
                {"memories": [{"key": "home_city", "value": "杭州", "category": "user_identity"}]}
            ),
            json_obj({"relation": "conflict", "reason": "搬家"}),
        ]
    )
    service = MemoryService(LLMClient(sdk), store)

    first = service.remember("我住在重庆", user_id=USER)
    second = service.remember("我还是住重庆", user_id=USER)
    third = service.remember("我搬到杭州了", user_id=USER)

    assert first.action_for("home_city") == "add"
    assert second.action_for("home_city") == "keep"  # 调了 adjudicator
    assert third.action_for("home_city") == "update"
    assert store.values(USER) == {"home_city": "杭州"}
    assert len(store.all(USER)) == 1  # 始终是一条，没有产生重复行
    assert len(sdk.calls) == 5  # 抽取×3 + 裁决×2，顺序与预期一致
