"""ReAct-style 引擎：原生 Function Calling + Thought 归一化。对应 design.md 第 7 节。

要点：

- 只使用原生 ``tools + tool_calls``，不自造 JSON Action 协议（design.md 7.1）
- ``thought``（= 原生 ``content``）可为空，缺失不报错（design.md 7.3，方案 A）
- Thought 一并写进 ``ReactResult.thoughts`` 与 Trace，**是否展示给用户由 CLI 决定**
- 回传消息顺序固定 ``assistant(tool_calls) → tool``（design.md 7.4）
- 并行工具调用有界并发、``call_id`` 配对
- 连续「相同工具 + 相同参数」的调用被拦截，不重复执行（design.md 7.5 保护规则）
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Sequence

from first_agent.core.client import LLMClient, LLMError
from first_agent.core.context import DEFAULT_SYSTEM_PROMPT
from first_agent.schemas.tool import ToolCall, ToolResult
from first_agent.tools.executor import ToolExecutor
from first_agent.tools.registry import ToolRegistry

DEFAULT_MAX_STEPS = 5
MAX_THOUGHT_CHARS = 500


@dataclass
class ReactResult:
    """一次 ReAct run 的结果，可直接喂给评测适配器。"""

    answer: str = ""
    thoughts: list[str] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    arguments: dict[str, list[dict]] = field(default_factory=dict)
    tool_results: list[ToolResult] = field(default_factory=list)
    # 本轮完整消息序列（含 assistant tool_calls 与 tool observation），供 SessionStore 持久化
    messages: list[dict] = field(default_factory=list)
    steps: int = 0
    latency_ms: int = 0
    status: str = "success"  # success / max_steps / error
    error: str | None = None


class ReactEngine:
    def __init__(
        self,
        client: LLMClient,
        registry: ToolRegistry,
        *,
        executor: ToolExecutor | None = None,
        system_prompt: str | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        tracer: Any = None,
        max_thought_chars: int = MAX_THOUGHT_CHARS,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps 至少为 1")
        self.client = client
        self.registry = registry
        self.executor = executor or ToolExecutor(registry, tracer=tracer)
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        self.max_steps = max_steps
        self.tracer = tracer
        self.max_thought_chars = max_thought_chars

    # -- 入口 -------------------------------------------------------------
    def run(self, user_input: str, *, history: Sequence[dict] | None = None) -> ReactResult:
        """独立入口：自行组装最小上下文（**不含**长期记忆注入）。

        需要动态记忆注入时走 ``core.engine.ToolAgentRuntime``，由它把组装好的
        消息交给 :meth:`run_messages`。
        """

        messages: list[dict] = [{"role": "system", "content": self.system_prompt}]
        if history:
            messages.extend(dict(message) for message in history)
        messages.append({"role": "user", "content": user_input})
        return self.run_messages(messages)

    # -- 主循环 -----------------------------------------------------------
    def run_messages(self, messages: list[dict]) -> ReactResult:
        started = time.perf_counter()
        messages = [dict(message) for message in messages]
        tool_schemas = self.registry.schemas()
        result = ReactResult()
        previous_keys: list[tuple[str, str]] | None = None

        for step in range(1, self.max_steps + 1):
            result.steps = step
            try:
                turn = self.client.chat_turn(messages, tools=tool_schemas)
            except LLMError as exc:
                result.status, result.error = "error", str(exc)
                break

            if not turn.tool_calls:
                # 没有工具调用：本轮 content 即最终回答（不记为 Thought）
                result.answer = turn.thought or ""
                messages.append({"role": "assistant", "content": result.answer})
                break

            # 只有「伴随工具调用」的 content 才是 Thought（design.md 7.5）
            if turn.thought:
                result.thoughts.append(turn.thought)
            self._trace_thought(turn.thought, step)

            # 原生协议要求：tool 消息之前必须先有携带 tool_calls 的 assistant 消息
            messages.append(self._assistant_message(turn.thought, turn.tool_calls))

            call_keys = [_call_key(call) for call in turn.tool_calls]
            if previous_keys is not None and call_keys == previous_keys:
                for call in turn.tool_calls:
                    result.tool_results.append(
                        ToolResult(
                            call_id=call.call_id,
                            ok=False,
                            tool_name=call.tool_name,
                            error_code="duplicate_call",
                            error_message="相同工具与参数刚刚已执行过，请基于已有结果作答",
                        )
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.call_id,
                            "content": _error_observation(
                                "duplicate_call", "该调用已执行过，请基于已有结果作答"
                            ),
                        }
                    )
                self._trace(
                    "react_duplicate_blocked",
                    step=step,
                    tools=[call.tool_name for call in turn.tool_calls],
                )
                continue
            previous_keys = call_keys

            results = self.executor.execute_all(turn.tool_calls)
            for call, tool_result in zip(turn.tool_calls, results):
                result.tool_calls.append(call.tool_name)
                result.arguments.setdefault(call.tool_name, []).append(call.arguments)
                result.tool_results.append(tool_result)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.call_id,
                        "content": _observation(tool_result),
                    }
                )
        else:
            result.status = "max_steps"
            result.answer = "抱歉，这个问题需要更多步骤才能完成，请补充更具体的信息。"

        result.latency_ms = int((time.perf_counter() - started) * 1000)
        result.messages = messages
        # 只记元信息：完整答案由 run_end 记录一次，避免 Trace 体积翻倍
        self._trace(
            "react_answer",
            status=result.status,
            steps=result.steps,
            tool_calls=result.tool_calls,
            latency_ms=result.latency_ms,
        )
        return result

    # -- 辅助 -------------------------------------------------------------
    @staticmethod
    def _assistant_message(thought: str | None, calls: Sequence[ToolCall]) -> dict:
        return {
            "role": "assistant",
            "content": thought,
            "tool_calls": [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {
                        "name": call.tool_name,
                        "arguments": json.dumps(call.arguments, ensure_ascii=False),
                    },
                }
                for call in calls
            ],
        }

    def _trace_thought(self, thought: str | None, step: int) -> None:
        # 没有 Thought 时不留空事件（避免 Trace 噪音）
        if self.tracer is None or not thought:
            return
        text = thought
        self._trace(
            "react_thought",
            step=step,
            thought=text[: self.max_thought_chars],
            thought_truncated=len(text) > self.max_thought_chars,
        )

    def _trace(self, kind: str, **payload: Any) -> None:
        if self.tracer is not None:
            self.tracer.log(kind, **payload)


def _observation(tool_result: ToolResult) -> str:
    """把 ToolResult 转成回传给模型的 Observation 文本。"""

    if tool_result.ok:
        return json.dumps(tool_result.data, ensure_ascii=False, default=str)
    return _error_observation(tool_result.error_code or "tool_error", tool_result.error_message or "")


def _error_observation(error_code: str, message: str) -> str:
    return json.dumps({"error": error_code, "message": message}, ensure_ascii=False)


def _call_key(call: ToolCall) -> tuple[str, str]:
    """用于「连续重复调用」判定的规范化键。"""

    return (
        call.tool_name,
        json.dumps(call.arguments, sort_keys=True, ensure_ascii=False, default=str),
    )
