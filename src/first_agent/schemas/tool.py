"""工具契约：ToolSpec / ToolCall / ToolResult 与运行时归一化结构。

对应 design.md 第 7 节（原生 Function Calling 与内部归一化）与第 11 节（工具契约）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    """一次工具调用，由原生 ``tool_calls`` 归一化而来（design.md 7.2）。"""

    call_id: str
    tool_name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    """工具执行结果。

    ``data`` 固定为 ``output_model`` 校验后 ``model_dump()`` 的 dict，
    ``$steps.<id>.<field>`` 的 field 即它的键（design.md 11.2）。
    """

    call_id: str
    ok: bool
    tool_name: str
    data: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    latency_ms: int = 0


class NormalizedAssistantTurn(BaseModel):
    """原生响应的内部投影，不是模型输出契约。

    ``thought`` 即原生 ``content``：模型同一次响应里的自然语言说明，可为空
    （design.md 7.3「Thought 可选」）。
    """

    thought: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)


def _strip_titles(node: Any) -> Any:
    """递归移除 JSON Schema 中的 ``title`` 字段，降低 tool schema 的 token 占用。"""

    if isinstance(node, dict):
        return {key: _strip_titles(value) for key, value in node.items() if key != "title"}
    if isinstance(node, list):
        return [_strip_titles(item) for item in node]
    return node


def _inline_defs(schema: dict[str, Any]) -> dict[str, Any]:
    """把 ``$defs`` 中的 ``$ref`` 内联展开。

    pydantic 对 Enum / 嵌套模型会生成 ``{"$ref": "#/$defs/X"}``，而部分
    函数调用实现（含 DeepSeek）对 ``$ref`` 的处理并不一致；内联后 schema
    自包含，``enum`` / ``default`` 等约束对模型直接可见。
    """

    defs = schema.pop("$defs", {})

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            reference = node.get("$ref")
            if isinstance(reference, str) and reference.startswith("#/$defs/"):
                name = reference[len("#/$defs/") :]
                merged = dict(resolve(defs.get(name, {})))
                merged.update({k: resolve(v) for k, v in node.items() if k != "$ref"})
                return merged
            return {key: resolve(value) for key, value in node.items()}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        return node

    return resolve(schema)


@dataclass
class ToolSpec:
    """工具的显式契约：输入/输出模型 + 处理函数 + 超时与重试策略。

    input_model / output_model 之外的字段均为执行策略，由 ToolRegistry 统一执行时使用。
    """

    name: str
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: Callable[..., Any]
    timeout_s: float = 5.0
    max_retries: int = 1
    side_effect: bool = False

    def __post_init__(self) -> None:
        for field_name in ("input_model", "output_model"):
            model = getattr(self, field_name)
            if not (isinstance(model, type) and issubclass(model, BaseModel)):
                raise TypeError(
                    f"ToolSpec.{field_name} 必须是 pydantic BaseModel 子类，收到 {model!r}"
                )
        if not callable(self.handler):
            raise TypeError("ToolSpec.handler 必须可调用")
        if self.timeout_s <= 0:
            raise ValueError("ToolSpec.timeout_s 必须为正数")
        if self.max_retries < 0:
            raise ValueError("ToolSpec.max_retries 不能为负数")

    def parameters_schema(self) -> dict[str, Any]:
        """输入模型的 JSON Schema（已内联 ``$defs``、剥离 ``title``）。

        保留 default / description / enum / required（design.md 11.4）。
        """

        return _strip_titles(_inline_defs(self.input_model.model_json_schema()))

    def to_function_schema(self) -> dict[str, Any]:
        """生成 OpenAI / DeepSeek function calling 所需的工具描述。"""

        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema(),
            },
        }
