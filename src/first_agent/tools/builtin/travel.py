"""出行耗时工具（design.md 11.5）。

双参数 + 可形成依赖（例如先 ``resolve_datetime`` 再 ``estimate_travel_time``），
是验证 Plan 依赖链的关键工具。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from first_agent.providers.base import TravelProvider
from first_agent.schemas.tool import ToolSpec


class TravelTimeInput(BaseModel):
    origin: str = Field(description="出发地，例如 家、解放碑")
    destination: str = Field(description="目的地，例如 公司、江北机场")
    mode: str = Field(
        default="car",
        description="出行方式：walk / bike / bus / subway / car / taxi",
    )


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
    )
