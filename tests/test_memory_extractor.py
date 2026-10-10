"""记忆抽取器测试：别名规范化、逐条容错与失败降级（design.md 12.3 / 13）。"""

from __future__ import annotations

from wayfinder.core.client import LLMClient
from wayfinder.evals.scripted import ScriptedLLM, json_obj, json_raw
from wayfinder.memory.extractor import (
    CANONICAL_KEYS,
    MemoryExtractor,
    normalize_category,
    normalize_key,
)


def make_extractor(script, **kwargs) -> MemoryExtractor:
    return MemoryExtractor(LLMClient(ScriptedLLM(script)), **kwargs)


def extraction(*items: dict) -> dict:
    return {"memories": list(items)}


# -- 规范化 ----------------------------------------------------------------


def test_normalize_key_maps_aliases():
    assert normalize_key("居住地") == "home_city"
    assert normalize_key("用户城市") == "home_city"
    assert normalize_key("姓名") == "user_name"
    assert normalize_key("HOME_CITY") == "home_city"  # 大小写不敏感
    assert normalize_key("  home_city  ") == "home_city"


def test_dietary_preference_aliases_are_recognized():
    """真实 v1 库里存在「饮食偏好」，必须能识别，否则抽取阶段会静默丢弃。"""

    assert normalize_key("饮食偏好") == "dietary_preference"
    assert normalize_key("忌口") == "dietary_preference"
    assert normalize_key("口味") == "dietary_preference"


def test_normalize_key_returns_none_for_unknown():
    assert normalize_key("favorite_color") is None
    assert normalize_key("") is None
    assert normalize_key("   ") is None


def test_normalize_category_maps_aliases():
    assert normalize_category("用户身份") == "user_identity"
    assert normalize_category("偏好") == "preference"
    assert normalize_category("习惯") == "habit"
    assert normalize_category("habit") == "habit"


def test_normalize_category_falls_back_to_preference():
    assert normalize_category("无法识别的分类") == "preference"
    assert normalize_category("") == "preference"


def test_canonical_keys_cover_all_supported_fields():
    assert set(CANONICAL_KEYS) == {
        "home_city",
        "timezone",
        "transport_mode",
        "user_name",
        "dietary_preference",
        "long_term_plan",
    }


def test_extract_keeps_dietary_preference():
    extractor = make_extractor(
        [json_obj(extraction({"key": "饮食偏好", "value": "不吃辣", "category": "用户偏好"}))]
    )

    outcome = extractor.extract("我不吃辣", user_id="u1")

    assert outcome.keys == ["dietary_preference"]
    assert outcome.candidates[0].category == "preference"


def test_extract_keeps_all_native_keys():
    """六个规范 key 都要能落下来（防止白名单与提示词不同步）。"""

    extractor = make_extractor(
        [
            json_obj(
                extraction(
                    *[
                        {"key": key, "value": f"v-{key}", "category": "preference"}
                        for key in CANONICAL_KEYS
                    ]
                )
            )
        ]
    )

    outcome = extractor.extract("随便说说", user_id="u1")

    assert set(outcome.keys) == set(CANONICAL_KEYS)
    assert outcome.dropped == []


# -- 抽取 ------------------------------------------------------------------


def test_extract_returns_candidates_with_injected_user_id():
    extractor = make_extractor(
        [
            json_obj(
                extraction(
                    {"key": "home_city", "value": "重庆", "category": "user_identity"},
                    {"key": "user_name", "value": "张三", "category": "user_identity"},
                )
            )
        ]
    )

    outcome = extractor.extract("我叫张三，住在重庆", user_id="u1")

    assert outcome.failed is False
    assert outcome.keys == ["home_city", "user_name"]
    assert all(candidate.user_id == "u1" for candidate in outcome.candidates)
    assert outcome.candidates[0].category == "user_identity"


def test_extract_normalizes_chinese_key_and_category():
    extractor = make_extractor(
        [json_obj(extraction({"key": "居住地", "value": "重庆", "category": "用户身份"}))]
    )

    outcome = extractor.extract("我住在重庆", user_id="u1")

    assert outcome.keys == ["home_city"]
    assert outcome.candidates[0].category == "user_identity"


def test_unknown_key_is_dropped_with_reason():
    extractor = make_extractor(
        [
            json_obj(
                extraction(
                    {"key": "home_city", "value": "重庆", "category": "user_identity"},
                    {"key": "favorite_color", "value": "蓝", "category": "preference"},
                )
            )
        ]
    )

    outcome = extractor.extract("我住在重庆，喜欢蓝色", user_id="u1")

    assert outcome.keys == ["home_city"]
    assert outcome.dropped[0]["reason"] == "unknown_key"
    assert outcome.dropped[0]["key"] == "favorite_color"


def test_empty_value_is_dropped():
    extractor = make_extractor(
        [json_obj(extraction({"key": "home_city", "value": "   ", "category": "user_identity"}))]
    )

    outcome = extractor.extract("我住在一个地方", user_id="u1")

    assert outcome.candidates == []
    assert outcome.dropped[0]["reason"] == "empty_value"


def test_one_bad_item_does_not_kill_the_batch():
    extractor = make_extractor(
        [
            json_obj(
                extraction(
                    {"key": "user_name", "value": "张三", "category": "user_identity"},
                    {"key": "home_city", "value": "", "category": "user_identity"},
                    {"key": "timezone", "value": "Asia/Shanghai", "category": "preference"},
                )
            )
        ]
    )

    outcome = extractor.extract("我叫张三，住重庆，用东八区", user_id="u1")

    assert outcome.keys == ["user_name", "timezone"]  # 中间那条坏的不影响其他两条
    assert len(outcome.dropped) == 1


def test_duplicate_key_keeps_last_value():
    extractor = make_extractor(
        [
            json_obj(
                extraction(
                    {"key": "home_city", "value": "重庆", "category": "user_identity"},
                    {"key": "居住地", "value": "杭州", "category": "用户身份"},
                )
            )
        ]
    )

    outcome = extractor.extract("我以前住重庆，现在住杭州", user_id="u1")

    assert outcome.keys == ["home_city"]  # 去重
    assert outcome.candidates[0].value == "杭州"  # 保留最后表述
    assert outcome.dropped[0]["reason"] == "duplicate_key"


def test_empty_memories_list_yields_nothing():
    extractor = make_extractor([json_obj(extraction())])

    outcome = extractor.extract("今天天气不错", user_id="u1")

    assert outcome.candidates == []
    assert outcome.dropped == []
    assert outcome.failed is False


# -- 失败与重试 ------------------------------------------------------------


def test_invalid_json_then_valid_is_recovered_by_retry():
    extractor = make_extractor(
        [
            json_raw("这不是 JSON"),
            json_obj(extraction({"key": "home_city", "value": "重庆", "category": "user_identity"})),
        ]
    )

    outcome = extractor.extract("我住在重庆", user_id="u1")

    assert outcome.keys == ["home_city"]


def test_total_failure_returns_empty_and_is_flagged():
    extractor = make_extractor([json_raw("bad"), json_raw("still bad")])

    outcome = extractor.extract("我住在重庆", user_id="u1")

    assert outcome.candidates == []
    assert outcome.failed is True  # 上层据此 no-op，绝不污染数据库


def test_blank_input_short_circuits_without_calling_llm():
    sdk = ScriptedLLM([json_obj(extraction())])
    extractor = MemoryExtractor(LLMClient(sdk))

    outcome = extractor.extract("   ", user_id="u1")

    assert outcome.candidates == []
    assert sdk.calls == []  # 没有浪费一次 LLM 调用


def test_long_input_is_truncated():
    sdk = ScriptedLLM([json_obj(extraction())])
    extractor = MemoryExtractor(LLMClient(sdk), max_input_chars=5)

    extractor.extract("超长的输入内容" * 100, user_id="u1")

    user_message = sdk.calls[0]["messages"][-1]
    assert user_message["role"] == "user"
    assert len(user_message["content"]) == 5


def test_tracer_receives_dropped_events():
    class Recorder:
        def __init__(self) -> None:
            self.events: list[str] = []

        def log(self, kind, **payload):
            self.events.append(kind)

    recorder = Recorder()
    extractor = make_extractor(
        [json_obj(extraction({"key": "favorite_color", "value": "蓝", "category": "preference"}))],
        tracer=recorder,
    )

    extractor.extract("我喜欢蓝色", user_id="u1")

    assert "memory_extract_dropped" in recorder.events
