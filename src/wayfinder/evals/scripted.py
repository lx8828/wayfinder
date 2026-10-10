"""可脚本化的假 LLM，用于离线测试 Runtime 机制（design.md 14.4）。

实现 OpenAI 兼容 SDK 的最小接口 ``.chat.completions.create``，因此可以直接注入
``LLMClient``，**不需要 API Key、不联网**。

脚本是一个「步骤」列表，每次 ``create`` 消耗一个步骤：

- ``text("...")``：最终回答（``content``，无 ``tool_calls``）
- ``tool("get_weather", {"city": "重庆"}, thought="先查天气")``：一次工具调用
- ``tools([...], thought=...)``：一次并行多工具调用
- ``json_obj({...})`` / ``json_raw("...")``：结构化输出（用于 ``chat_json``）
- ``fail(TimeoutError("boom"))``：抛错，模拟超时/网络异常
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Iterable, Sequence


@dataclass
class ToolCallSpec:
    name: str
    arguments: dict[str, Any] | str = field(default_factory=dict)
    call_id: str | None = None


@dataclass
class TextStep:
    content: str


@dataclass
class ToolStep:
    calls: list[ToolCallSpec]
    thought: str | None = None


@dataclass
class JsonStep:
    payload: Any = None
    raw: str | None = None


@dataclass
class ErrorStep:
    error: BaseException


# -- 脚本构造辅助 ----------------------------------------------------------


def text(content: str) -> TextStep:
    return TextStep(content)


def tool(name: str, arguments: dict[str, Any] | str | None = None, *, thought: str | None = None,
         call_id: str | None = None) -> ToolStep:
    return ToolStep(calls=[ToolCallSpec(name, arguments or {}, call_id)], thought=thought)


def tools(specs: Sequence[ToolCallSpec], *, thought: str | None = None) -> ToolStep:
    return ToolStep(calls=list(specs), thought=thought)


def json_obj(payload: Any) -> JsonStep:
    return JsonStep(payload=payload)


def json_raw(raw: str) -> JsonStep:
    return JsonStep(raw=raw)


def fail(error: BaseException) -> ErrorStep:
    return ErrorStep(error)


class ScriptedLLM:
    """按脚本返回响应的假 SDK。"""

    def __init__(
        self,
        script: Iterable[Any] = (),
        *,
        default_text: str = "（脚本已用尽）",
        strict: bool = True,
    ) -> None:
        self._script = list(script)
        self.default_text = default_text
        self.strict = strict
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    # -- SDK 接口 ---------------------------------------------------------
    def _create(self, **kwargs):
        # 深拷贝请求快照：调用方之后可能继续 mutate messages，不能让它污染记录
        self.calls.append(copy.deepcopy(kwargs))
        step_index = len(self.calls)
        step = self._script.pop(0) if self._script else TextStep(self.default_text)

        if isinstance(step, ErrorStep):
            raise step.error

        is_json_request = kwargs.get("response_format") == {"type": "json_object"}
        if self.strict and is_json_request and not isinstance(step, JsonStep):
            raise AssertionError(
                f"脚本第 {step_index} 步与实际请求不匹配：请求为 JSON mode，但脚本给的是 "
                f"{type(step).__name__}。请改用 json_obj() / json_raw()。"
            )

        if isinstance(step, JsonStep):
            content = (
                step.raw
                if step.raw is not None
                else json.dumps(step.payload, ensure_ascii=False)
            )
            return self._response(content, tool_calls=None)

        if isinstance(step, ToolStep):
            native_calls = [
                _to_native_call(spec, index, step_index)
                for index, spec in enumerate(step.calls)
            ]
            return self._response(step.thought, tool_calls=native_calls)

        if isinstance(step, TextStep):
            return self._response(step.content, tool_calls=None)

        raise TypeError(f"不支持的脚本步骤类型：{type(step).__name__}")

    # -- 辅助 -------------------------------------------------------------
    @staticmethod
    def _response(content: str | None, tool_calls: list | None):
        message = SimpleNamespace(content=content, tool_calls=tool_calls)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message)],
            usage=SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0),
        )


def _to_native_call(spec: ToolCallSpec, index: int, step_index: int):
    call_id = spec.call_id or f"call_{step_index}_{index}"
    arguments = (
        spec.arguments
        if isinstance(spec.arguments, str)
        else json.dumps(spec.arguments, ensure_ascii=False)
    )
    return SimpleNamespace(
        id=call_id,
        type="function",
        function=SimpleNamespace(name=spec.name, arguments=arguments),
    )
