"""基础设施层：LLM 客户端与 Trace。

ContextBuilder / SessionStore / Runtime 门面在后续阶段加入（design.md 第 5 节）。
"""

from first_agent.core.client import JSONValidationError, LLMClient, LLMError
from first_agent.core.tracing import (
    TraceLogger,
    hash_identifier,
    mask_sensitive,
    mask_text,
    sanitize,
)

__all__ = [
    "LLMClient",
    "LLMError",
    "JSONValidationError",
    "TraceLogger",
    "hash_identifier",
    "mask_text",
    "mask_sensitive",
    "sanitize",
]
