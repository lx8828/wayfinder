"""MemoryStore 测试：确定性动作裁决与 UserProfile（design.md 12）。"""

from __future__ import annotations

from wayfinder.core.db import Database
from wayfinder.memory.store import MemoryStore
from wayfinder.schemas.memory import MemoryCandidate


def make_store() -> MemoryStore:
    return MemoryStore(Database(":memory:"))


def candidate(
    key: str = "home_city", value: str = "重庆", category: str = "user_identity", user_id: str = "u1"
) -> MemoryCandidate:
    return MemoryCandidate(user_id=user_id, key=key, value=value, category=category)


def test_add_when_no_previous_value():
    store = make_store()

    assert store.apply_candidate(candidate()) == "add"
    assert store.get("u1", "home_city").value == "重庆"


def test_missing_relation_but_existing_key_is_treated_as_conflict():
    """调用方漏判旧值时不能静默覆盖成新 key，也不能丢数据。"""

    store = make_store()
    store.apply_candidate(candidate())

    assert store.apply_candidate(candidate(value="杭州")) == "update"
    assert store.get("u1", "home_city").value == "杭州"


def test_relation_mapping_is_deterministic():
    store = make_store()
    store.apply_candidate(candidate())

    assert store.apply_candidate(candidate(value="重庆"), "same") == "keep"
    assert store.apply_candidate(candidate(value="杭州"), "conflict") == "update"
    assert store.apply_candidate(candidate(value="爱甜食"), "complementary") == "skip"
    assert store.apply_candidate(candidate(value="多值结构"), "unsupported") == "skip"
    assert store.apply_candidate(candidate(value="随便"), "unknown") == "skip"
    assert store.get("u1", "home_city").value == "杭州"  # skip 不落库


def test_keep_refreshes_last_seen():
    store = make_store()
    store.apply_candidate(candidate())
    before = store.get("u1", "home_city").last_seen_at

    store.mark_seen("u1", "home_city")

    assert store.get("u1", "home_city").last_seen_at >= before


def test_profile_view_exposes_typed_accessors():
    store = make_store()
    store.apply_candidate(candidate())
    store.apply_candidate(candidate(key="timezone", value="Asia/Shanghai", category="preference"))

    profile = store.profile("u1")

    assert profile.home_city == "重庆"
    assert profile.timezone == "Asia/Shanghai"
    assert profile.user_name is None
    assert not profile.is_empty()
    assert profile.as_items() == [("home_city", "重庆"), ("timezone", "Asia/Shanghai")]


def test_profile_of_unknown_user_is_empty():
    assert make_store().profile("nobody").is_empty()


def test_values_are_isolated_per_user():
    store = make_store()
    store.apply_candidate(candidate())
    store.apply_candidate(candidate(value="北京", user_id="u2"))

    assert store.values("u1") == {"home_city": "重庆"}
    assert store.values("u2") == {"home_city": "北京"}
