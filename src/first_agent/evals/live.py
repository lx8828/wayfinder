"""真 LLM 评测装配（design.md 16.5 的「波动指标」段）。

与离线段**唯一**的区别是换成真实模型：

- 外部世界仍是 Fake provider + 固定基准日 → 指标波动只可能来自模型
- 仍然是每个用例一个 ``:memory:`` 数据库
- 波动指标只告警，不进硬门禁

这样设计的原因：若同时换掉模型与外部服务，就无法判断指标变化是模型变差还是
「天气 API 今天返回了别的内容」。
"""

from __future__ import annotations

import os
from typing import Any

from first_agent.core.client import LLMClient
from first_agent.evals.case import EvalCase
from first_agent.evals.offline import build_registry, build_runtime
from first_agent.evals.runner import Engine, EngineFactory
from first_agent.evals.adapters import runtime_engine

DEFAULT_MODEL = "deepseek-chat"
DEFAULT_BASE_URL = "https://api.deepseek.com"


def require_api_key() -> str:
    """读取 ``DEEPSEEK_API_KEY``；缺失时给出可执行的提示。"""

    from dotenv import load_dotenv

    load_dotenv()
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise RuntimeError(
            "真 LLM 评测需要 DEEPSEEK_API_KEY（可写在 .env 或导出为环境变量）。"
            "离线评测（不加 --real）不需要任何密钥。"
        )
    return api_key


def build_live_client(*, tracer: Any = None) -> LLMClient:
    from openai import OpenAI

    sdk = OpenAI(
        api_key=require_api_key(),
        base_url=os.getenv("DEEPSEEK_BASE_URL", DEFAULT_BASE_URL),
    )
    return LLMClient(
        sdk, model=os.getenv("DEEPSEEK_MODEL", DEFAULT_MODEL), tracer=tracer
    )


def live_engine_factory(*, client: LLMClient | None = None, tracer: Any = None) -> EngineFactory:
    """``EngineFactory``：真模型 + Fake 世界 + 独立 DB。"""

    shared = client or build_live_client(tracer=tracer)

    def factory(case: EvalCase) -> Engine:
        return runtime_engine(build_runtime(case, client=shared, tracer=tracer))

    return factory


__all__ = [
    "DEFAULT_BASE_URL",
    "DEFAULT_MODEL",
    "build_live_client",
    "live_engine_factory",
    "require_api_key",
]
