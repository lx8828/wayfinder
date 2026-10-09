"""LLM 客户端封装：超时、结构化输出状态机与响应归一化。

对应 design.md 第 12 节（LLM Client）与第 13 节（JSON 输出状态机）。

设计要点：

- 只包装一个 OpenAI 兼容 SDK 对象（``sdk.chat.completions.create``），
  因此可以在测试中注入 ``ScriptedLLM``，无需 API Key。
- ReAct 走原生 Function Calling：``chat_turn`` 把响应归一化为
  ``thought + tool_calls``（design.md 7.2 / 7.3），**不**自造 JSON Action 协议。
- Plan / Memory 走 ``chat_json``：解析 → 校验 → 错误回灌 → 重试 → 抛错降级。
"""

from __future__ import annotations

import json
import time
from typing import Any

from pydantic import BaseModel, ValidationError

from first_agent.schemas.tool import NormalizedAssistantTurn, ToolCall


class LLMError(RuntimeError):
    """LLM 调用层错误基类。"""


class JSONValidationError(LLMError):
    """``chat_json`` 达到最大调用次数后仍无法得到合法结构化输出。"""

    def __init__(
        self,
        schema_name: str,
        *,
        attempts: int,
        last_raw: str | None,
        last_error: Exception | None,
    ) -> None:
        self.schema_name = schema_name
        self.attempts = attempts
        self.last_raw = last_raw
        self.last_error = last_error
        super().__init__(
            f"chat_json 在 {attempts} 次尝试后仍无法通过 {schema_name} 校验：{last_error}"
        )


def _repair_prompt(error: Exception, schema: type[BaseModel]) -> str:
    """把校验错误回灌给模型，要求只输出修正后的 JSON。"""

    return (
        "上一次输出无法通过 JSON Schema 校验。\n"
        f"校验错误：{error}\n"
        f"目标结构：{json.dumps(schema.model_json_schema(), ensure_ascii=False)}\n"
        "请只输出修正后的 JSON 对象，不要包含解释、Markdown 代码块或任何多余文本。"
    )


def _loads_arguments(raw: Any) -> dict[str, Any]:
    """原生 ``function.arguments`` 是 JSON 字符串；解析失败返回空 dict，交由参数校验拦截。"""

    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class LLMClient:
    """对 OpenAI 兼容 SDK 的薄封装。"""

    def __init__(
        self,
        sdk: Any,
        *,
        model: str = "deepseek-chat",
        timeout_s: float = 30.0,
        max_json_calls: int = 2,
        tracer: Any = None,
    ) -> None:
        if max_json_calls < 1:
            raise ValueError("max_json_calls 至少为 1")
        self.sdk = sdk
        self.model = model
        self.timeout_s = timeout_s
        self.max_json_calls = max_json_calls
        self.tracer = tracer

    # -- 原生调用 --------------------------------------------------------
    def _create(
        self,
        messages: list[dict],
        *,
        json_mode: bool = False,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
    ) -> Any:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "timeout": self.timeout_s,
        }
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        if tools is not None:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"

        started = time.perf_counter()
        try:
            response = self.sdk.chat.completions.create(**kwargs)
        except Exception as exc:  # 网络 / 超时 / 鉴权等
            self._trace("llm_error", error=repr(exc), latency_ms=self._elapsed_ms(started))
            raise LLMError(f"LLM 调用失败：{exc}") from exc
        self._trace(
            "llm_call",
            json_mode=json_mode,
            has_tools=tools is not None,
            latency_ms=self._elapsed_ms(started),
            usage=self._usage(response),
        )
        return response

    def chat_text(self, messages: list[dict]) -> str:
        """纯文本对话（无工具）。"""

        response = self._create(messages)
        return response.choices[0].message.content or ""

    def chat_turn(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
    ) -> NormalizedAssistantTurn:
        """一次原生 Function Calling，归一化为 thought + tool_calls（design.md 7.2）。"""

        response = self._create(messages, tools=tools, tool_choice=tool_choice)
        message = response.choices[0].message
        tool_calls = [
            ToolCall(
                call_id=call.id,
                tool_name=call.function.name,
                arguments=_loads_arguments(call.function.arguments),
            )
            for call in (getattr(message, "tool_calls", None) or [])
        ]
        return NormalizedAssistantTurn(thought=message.content, tool_calls=tool_calls)

    def chat_json(self, messages: list[dict], schema: type[BaseModel]) -> BaseModel:
        """结构化输出状态机（design.md 13）。

        最多调用 ``max_json_calls`` 次；仍失败则抛 ``JSONValidationError``，
        由调用方决定降级（Plan → 追问/ReAct；Memory → no-op）。
        """

        conversation = list(messages)
        last_raw: str | None = None
        last_error: Exception | None = None

        for attempt in range(1, self.max_json_calls + 1):
            response = self._create(conversation, json_mode=True)
            raw = response.choices[0].message.content or ""
            last_raw = raw
            try:
                return schema.model_validate_json(raw)
            except ValidationError as exc:
                last_error = exc
                self._trace(
                    "chat_json_invalid",
                    attempt=attempt,
                    schema=schema.__name__,
                    error=str(exc).splitlines()[0] if str(exc) else "",
                )
                if attempt < self.max_json_calls:
                    conversation = conversation + [
                        {"role": "assistant", "content": raw},
                        {"role": "user", "content": _repair_prompt(exc, schema)},
                    ]

        raise JSONValidationError(
            schema.__name__,
            attempts=self.max_json_calls,
            last_raw=last_raw,
            last_error=last_error,
        )

    # -- 辅助 ------------------------------------------------------------
    @staticmethod
    def _usage(response: Any) -> dict[str, Any] | None:
        usage = getattr(response, "usage", None)
        if usage is None:
            return None
        return {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
        }

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return int((time.perf_counter() - started) * 1000)

    def _trace(self, kind: str, **payload: Any) -> None:
        if self.tracer is not None:
            self.tracer.log(kind, **payload)
