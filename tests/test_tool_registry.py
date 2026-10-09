"""ToolSpec 与 ToolRegistry 测试（design.md 6 / 11）。"""

from __future__ import annotations

from enum import Enum

import pytest
from pydantic import BaseModel, Field

from first_agent.schemas.tool import ToolSpec
from first_agent.tools.registry import ToolRegistry


class Mood(str, Enum):
    sunny = "sunny"
    rainy = "rainy"


class WeatherIn(BaseModel):
    city: str = Field(description="城市名，如 重庆")
    date: str | None = Field(default=None, description="日期，默认今天")
    mood: Mood = Field(default=Mood.sunny, description="仅供测试的枚举")


class WeatherOut(BaseModel):
    city: str
    temp_c: float


def handler(payload: WeatherIn) -> WeatherOut:
    return WeatherOut(city=payload.city, temp_c=1.0)


def make_spec(name: str = "get_weather") -> ToolSpec:
    return ToolSpec(
        name=name,
        description="查询天气",
        input_model=WeatherIn,
        output_model=WeatherOut,
        handler=handler,
    )


def test_register_and_lookup():
    registry = ToolRegistry()
    registry.register(make_spec())

    assert "get_weather" in registry
    assert len(registry) == 1
    assert registry.names() == ["get_weather"]
    assert registry.get("get_weather").description == "查询天气"
    assert registry.get("nope") is None


def test_duplicate_registration_is_rejected():
    registry = ToolRegistry()
    registry.register(make_spec())
    with pytest.raises(ValueError, match="重复注册"):
        registry.register(make_spec())


def test_register_rejects_non_spec():
    with pytest.raises(TypeError):
        ToolRegistry().register({"name": "x"})


def test_function_schema_reflects_required_default_and_enum():
    registry = ToolRegistry()
    registry.register(make_spec())
    schema = registry.schemas()[0]

    assert schema["type"] == "function"
    function = schema["function"]
    assert function["name"] == "get_weather"
    assert function["description"] == "查询天气"

    params = function["parameters"]
    assert set(params["properties"]) == {"city", "date", "mood"}
    assert params["required"] == ["city"]  # 有默认值的字段不进 required
    assert params["properties"]["city"]["description"] == "城市名，如 重庆"
    assert params["properties"]["date"]["default"] is None
    # Enum 已内联展开，模型可直接看到取值约束
    assert params["properties"]["mood"]["enum"] == ["sunny", "rainy"]
    assert params["properties"]["mood"]["type"] == "string"
    # $defs 与 title 均已剥离
    assert "$defs" not in params
    assert "title" not in params["properties"]["city"]


def test_toolspec_rejects_non_model_types():
    with pytest.raises(TypeError):
        ToolSpec(
            name="x",
            description="",
            input_model=dict,
            output_model=WeatherOut,
            handler=handler,
        )


def test_toolspec_rejects_bad_policy():
    with pytest.raises(ValueError):
        ToolSpec(
            name="x",
            description="",
            input_model=WeatherIn,
            output_model=WeatherOut,
            handler=handler,
            timeout_s=0,
        )
    with pytest.raises(ValueError):
        ToolSpec(
            name="x",
            description="",
            input_model=WeatherIn,
            output_model=WeatherOut,
            handler=handler,
            max_retries=-1,
        )


def test_toolspec_rejects_non_callable_handler():
    with pytest.raises(TypeError):
        ToolSpec(
            name="x",
            description="",
            input_model=WeatherIn,
            output_model=WeatherOut,
            handler="not callable",
        )
