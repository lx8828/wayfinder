"""基础设施层：LLM 客户端、上下文构建、会话存储与 Trace。

注意：组合根 ``core.engine`` **不在此处导入**。它依赖 ``runtime`` 与 ``memory``，
若被包顶层拉进来，会与 ``runtime.react`` 形成导入环
（``runtime.react → core.client → core.__init__ → core.engine → runtime.react``）。

需要门面时显式导入：

    from wayfinder.core.engine import ToolAgentRuntime
"""

from wayfinder.core.budget import (
    DEFAULT_BUDGET_MS,
    BudgetExceeded,
    RunBudget,
    build_partial_answer,
    check_budget,
)
from wayfinder.core.client import JSONValidationError, LLMClient, LLMError
from wayfinder.core.context import (
    DEFAULT_SYSTEM_PROMPT,
    MAX_CONTEXT_TOKENS,
    RECENT_TURNS_KEEP,
    SUMMARY_TRIGGER_TOKENS,
    SUMMARY_TRIGGER_TURNS,
    ContextBuilder,
    ContextInputs,
    estimate_tokens,
)
from wayfinder.core.db import Database, now_iso
from wayfinder.core.session import SessionRef, SessionResolver, SessionStore
from wayfinder.core.tracing import (
    TraceLogger,
    hash_identifier,
    mask_sensitive,
    mask_text,
    sanitize,
)

__all__ = [
    # budget
    "RunBudget",
    "BudgetExceeded",
    "DEFAULT_BUDGET_MS",
    "check_budget",
    "build_partial_answer",
    # client
    "LLMClient",
    "LLMError",
    "JSONValidationError",
    # context
    "ContextBuilder",
    "ContextInputs",
    "DEFAULT_SYSTEM_PROMPT",
    "MAX_CONTEXT_TOKENS",
    "RECENT_TURNS_KEEP",
    "SUMMARY_TRIGGER_TURNS",
    "SUMMARY_TRIGGER_TOKENS",
    "estimate_tokens",
    # db
    "Database",
    "now_iso",
    # session
    "SessionStore",
    "SessionResolver",
    "SessionRef",
    # tracing
    "TraceLogger",
    "hash_identifier",
    "mask_text",
    "mask_sensitive",
    "sanitize",
]
