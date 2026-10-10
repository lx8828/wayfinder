"""出行耗时工具（design.md 11.5）。

双参数 + 可形成依赖（例如先 ``resolve_datetime`` 再 ``estimate_travel_time``），
是验证 Plan 依赖链的关键工具。
"""

from __future__ import annotations

from typing import Literal, get_args

from pydantic import BaseModel, Field, field_validator

from first_agent.providers.base import TravelProvider
from first_agent.schemas.tool import ToolSpec

# 规范化后的出行方式（写进 schema 的 enum，模型能直接看到）
TravelMode = Literal["walk", "bike", "bus", "subway", "car", "taxi", "train"]

# 同义说法 → 规范值。真实模型不会只用枚举里的英文，常见的是「驾车 / 打车 / 高铁」。
# 别名在**工具契约层**归一化：schema 仍只暴露规范值，provider 永远收到规范值。
MODE_ALIASES: dict[str, str] = {
    # 中文
    "步行": "walk",
    "走路": "walk",
    "骑行": "bike",
    "自行车": "bike",
    "单车": "bike",
    "公交": "bus",
    "公交车": "bus",
    "巴士": "bus",
    "大巴": "bus",
    "地铁": "subway",
    "开车": "car",
    "驾车": "car",
    "自驾": "car",
    "汽车": "car",
    "打车": "taxi",
    "出租车": "taxi",
    "计程车": "taxi",
    "滴滴": "taxi",
    "高铁": "train",
    "动车": "train",
    "火车": "train",
    "列车": "train",
    # 英文同义
    "walking": "walk",
    "foot": "walk",
    "cycling": "bike",
    "bicycle": "bike",
    "driving": "car",
    "drive": "car",
    "metro": "subway",
    "underground": "subway",
    "cab": "taxi",
    "rail": "train",
    "hsr": "train",
    "high-speed rail": "train",
}


def normalize_mode(value: str) -> str:
    """把同义说法归一化成规范值；无法识别则抛 ``ValueError`` 并**列出可选值**。"""

    key = value.strip().lower()
    if key in MODE_ALIASES:
        return MODE_ALIASES[key]
    if key in get_args(TravelMode):
        return key
    legal = "/".join(get_args(TravelMode))
    raise ValueError(
        f"无法识别的出行方式 {value!r}；可选值：{legal}"
        "（也接受 开车 / 打车 / 地铁 / 公交 / 高铁 等同义说法）"
    )


class TravelTimeInput(BaseModel):
    origin: str = Field(description="出发地，例如 解放碑、江北机场")
    destination: str = Field(description="目的地，例如 公司、江北机场")
    # 必须是**枚举**而不是自由字符串：真实运行里模型传了描述之外的方式，
    # 结果 provider 抛 not_found（看起来像"查不到路线"，实际是契约违例）。
    # 约束写进 schema，模型在 function-calling 里就能看到合法取值；
    # 中英文别名在契约层归一化，尽量不让模型因为"说法不同"而失败。
    mode: TravelMode = Field(
        default="car", description="出行方式（也接受 开车 / 打车 / 地铁 / 公交 / 高铁 等说法）"
    )

    @field_validator("mode", mode="before")
    @classmethod
    def _normalize(cls, value: object) -> object:
        return normalize_mode(value) if isinstance(value, str) else value


class TravelTimeResult(BaseModel):
    origin: str
    destination: str
    mode: str
    duration_minutes: int
    source: str


def make_travel_time_tool(provider: TravelProvider) -> ToolSpec:
    def handler(payload: TravelTimeInput) -> dict:
        return dict(provider.estimate(payload.origin, payload.destination, payload.mode))

    return ToolSpec(
        name="estimate_travel_time",
        description="估算两地之间某种出行方式的耗时（分钟）",
        input_model=TravelTimeInput,
        output_model=TravelTimeResult,
        handler=handler,
        timeout_s=5.0,
        max_retries=1,
        slot_bindings={
            "origin": "origin",
            "destination": "destination",
            "mode": "transport_mode",
        },
    )
