"""v1 → v2 记忆迁移测试（design.md 18.2）。"""

from __future__ import annotations

import sqlite3

import pytest

from wayfinder.core.db import Database
from wayfinder.memory.migrate import (
    V1_SCHEMA,
    map_category,
    map_key,
    migrate,
    read_v1_memories,
)
from wayfinder.memory.store import MemoryStore

# 本机 v1 归档库里**实际出现过**的 key / category 组合
# （用真实数据验证映射表时发现的，见 design.md 0.1 的 R8）
REAL_V1_PAIRS = [
    ("用户姓名", "用户身份"),
    ("居住地", "用户身份"),
    ("饮食偏好", "用户偏好"),
    ("长期计划", "长期计划"),
]


def make_v1_db(path, rows):
    connection = sqlite3.connect(path)
    try:
        connection.executescript(V1_SCHEMA)
        for key, value, category in rows:
            connection.execute(
                "INSERT INTO memories (key, value, category) VALUES (?, ?, ?)",
                (key, value, category),
            )
        connection.commit()
    finally:
        connection.close()
    return path


def store_of(path) -> MemoryStore:
    return MemoryStore(Database(path))


# -- 映射 ------------------------------------------------------------------


def test_maps_chinese_keys_and_categories(tmp_path):
    v1 = make_v1_db(
        tmp_path / "memory.db",
        [("居住地", "重庆", "用户身份"), ("用户姓名", "张三", "用户身份")],
    )

    report = migrate(v1, tmp_path / "agent.db", user_id="demo", backup=False)

    assert report.total == 2
    assert report.added == 2
    assert ("居住地", "home_city") in report.renamed_keys
    assert ("用户身份", "user_identity") in report.renamed_categories
    assert report.warnings == []  # 全部是预期内映射，无告警

    store = store_of(tmp_path / "agent.db")
    assert store.values("demo") == {"home_city": "重庆", "user_name": "张三"}
    assert store.get("demo", "home_city").category == "user_identity"


def test_unknown_key_is_preserved_with_warning(tmp_path):
    v1 = make_v1_db(tmp_path / "memory.db", [("喜欢的颜色", "蓝色", "偏好")])

    report = migrate(v1, tmp_path / "agent.db", user_id="demo", backup=False)

    # 迁移不能丢数据：未知 key 原样保留
    assert store_of(tmp_path / "agent.db").values("demo") == {"喜欢的颜色": "蓝色"}
    assert any("未识别的 key" in warning for warning in report.warnings)


def test_unknown_category_falls_back_to_preference_with_warning(tmp_path):
    v1 = make_v1_db(tmp_path / "memory.db", [("home_city", "重庆", "神秘分类")])

    report = migrate(v1, tmp_path / "agent.db", user_id="demo", backup=False)

    assert store_of(tmp_path / "agent.db").get("demo", "home_city").category == "preference"
    assert report.renamed_categories == []
    assert any("未识别的 category" in warning for warning in report.warnings)


def test_expected_renames_are_not_reported_as_warnings(tmp_path):
    """规范化是预期行为，不能混进 WARN——否则真警告会被噪音淹没。"""

    v1 = make_v1_db(tmp_path / "memory.db", [("居住地", "重庆", "用户身份")])

    report = migrate(v1, tmp_path / "agent.db", user_id="demo", backup=False)

    assert report.warnings == []
    assert ("居住地", "home_city") in report.renamed_keys
    assert ("用户身份", "user_identity") in report.renamed_categories


def test_already_canonical_keys_produce_no_key_warning(tmp_path):
    v1 = make_v1_db(
        tmp_path / "memory.db",
        [("home_city", "重庆", "user_identity"), ("时区", "Asia/Shanghai", "偏好")],
    )

    report = migrate(v1, tmp_path / "agent.db", user_id="demo", backup=False)

    values = store_of(tmp_path / "agent.db").values("demo")
    assert values == {"home_city": "重庆", "timezone": "Asia/Shanghai"}
    assert report.warnings == []
    assert report.renamed_keys == [("时区", "timezone")]
    assert report.renamed_categories == [("偏好", "preference")]


@pytest.mark.parametrize(("key", "category"), REAL_V1_PAIRS)
def test_real_v1_key_and_category_pairs_are_all_recognized(key, category):
    """护栏：映射表不得再次落后于真实 v1 数据。"""

    _, key_status = map_key(key)
    _, category_status = map_category(category)

    assert key_status != "unknown", f"key {key} 不在映射表中"
    assert category_status != "unknown", f"category {category} 不在映射表中"


def test_dietary_preference_is_migrated_to_a_canonical_key(tmp_path):
    """真实 v1 库里存在「饮食偏好」；迁移后必须变成规范 key 并被 typed 视图读到。"""

    v1 = make_v1_db(tmp_path / "memory.db", [("饮食偏好", "不吃辣", "用户偏好")])

    report = migrate(v1, tmp_path / "agent.db", user_id="demo", backup=False)

    store = store_of(tmp_path / "agent.db")
    assert store.values("demo") == {"dietary_preference": "不吃辣"}
    assert store.get("demo", "dietary_preference").category == "preference"
    assert report.warnings == []
    assert ("饮食偏好", "dietary_preference") in report.renamed_keys
    assert ("用户偏好", "preference") in report.renamed_categories
    assert store.profile("demo").dietary_preference == "不吃辣"


# -- 备份与只读 ------------------------------------------------------------


def test_backup_is_created_and_original_untouched(tmp_path):
    v1 = make_v1_db(tmp_path / "memory.db", [("居住地", "重庆", "用户身份")])
    before = v1.read_bytes()

    report = migrate(v1, tmp_path / "agent.db", user_id="demo", backup=True)

    backup = tmp_path / "memory.db.v1.bak"
    assert backup.exists()
    assert backup.read_bytes() == before
    assert v1.read_bytes() == before  # 原库未被修改
    assert report.backup_path == str(backup)


def test_no_backup_when_disabled(tmp_path):
    v1 = make_v1_db(tmp_path / "memory.db", [("居住地", "重庆", "用户身份")])

    report = migrate(v1, tmp_path / "agent.db", backup=False)

    assert report.backup_path is None
    assert not (tmp_path / "memory.db.v1.bak").exists()


def test_read_v1_memories_is_read_only(tmp_path):
    v1 = make_v1_db(tmp_path / "memory.db", [("居住地", "重庆", "用户身份")])
    before = v1.read_bytes()

    rows = read_v1_memories(v1)

    assert rows == [{"key": "居住地", "value": "重庆", "category": "用户身份"}]
    assert v1.read_bytes() == before


# -- 幂等与边界 ------------------------------------------------------------


def test_migration_is_idempotent(tmp_path):
    v1 = make_v1_db(
        tmp_path / "memory.db",
        [("居住地", "重庆", "用户身份"), ("长期计划", "学英语", "长期计划")],
    )
    v2 = tmp_path / "agent.db"

    first = migrate(v1, v2, user_id="demo", backup=False)
    second = migrate(v1, v2, user_id="demo", backup=False)

    assert (first.added, first.updated) == (2, 0)
    assert (second.added, second.updated) == (0, 2)  # 第二次全部覆盖

    store = store_of(v2)
    assert len(store.all("demo")) == 2  # 没有产生重复行
    assert store.values("demo") == {"home_city": "重庆", "long_term_plan": "学英语"}


def test_empty_value_is_skipped_with_warning(tmp_path):
    v1 = make_v1_db(
        tmp_path / "memory.db",
        [("居住地", "   ", "用户身份"), ("用户姓名", "张三", "用户身份")],
    )

    report = migrate(v1, tmp_path / "agent.db", user_id="demo", backup=False)

    assert report.total == 2
    assert report.added == 1
    assert any("空 value" in warning for warning in report.warnings)
    assert store_of(tmp_path / "agent.db").values("demo") == {"user_name": "张三"}


def test_missing_v1_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        migrate(tmp_path / "nope.db", tmp_path / "agent.db", backup=False)


def test_source_is_marked_as_migration(tmp_path):
    v1 = make_v1_db(tmp_path / "memory.db", [("居住地", "重庆", "用户身份")])

    migrate(v1, tmp_path / "agent.db", user_id="demo", backup=False)

    assert store_of(tmp_path / "agent.db").get("demo", "home_city").source == "migration_v1"


def test_user_id_is_applied(tmp_path):
    v1 = make_v1_db(tmp_path / "memory.db", [("居住地", "重庆", "用户身份")])

    migrate(v1, tmp_path / "agent.db", user_id="alice", backup=False)
    store = store_of(tmp_path / "agent.db")

    assert store.values("alice") == {"home_city": "重庆"}
    assert store.values("demo") == {}


# -- 迁移的最终意义：迁完能被实际用上 --------------------------------------


def test_migrated_data_is_actionable_through_user_profile(tmp_path):
    v1 = make_v1_db(
        tmp_path / "memory.db",
        [("居住地", "重庆", "用户身份"), ("用户姓名", "张三", "用户身份")],
    )
    v2 = tmp_path / "agent.db"

    migrate(v1, v2, user_id="demo", backup=False)

    profile = store_of(v2).profile("demo")

    assert profile.home_city == "重庆"
    assert profile.user_name == "张三"
    assert not profile.is_empty()
