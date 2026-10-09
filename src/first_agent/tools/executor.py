"""工具执行器：参数校验、超时、重试与结构化结果。对应 design.md 11 / 14。

执行流水线（design.md 11.2）：

    arguments → input_model 校验 → handler → output_model 校验 → ToolResult.data(dict)

失败一律返回 ``ok=False`` 的 ``ToolResult``（不抛异常给上层），并带稳定的
``error_code``，供 Runtime 决定是否降级/重规划。
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from functools import partial
from typing import Any, Mapping, Sequence

from pydantic import ValidationError

from first_agent.providers.base import ProviderError
from first_agent.schemas.tool import ToolCall, ToolResult
from first_agent.tools.registry import ToolRegistry

# 重试也不会成功（或不该重试）的错误码
_NON_RETRYABLE = frozenset(
    {"invalid_arguments", "invalid_output", "unknown_tool", "not_found"}
)

# 专门用于「单次调用的超时」的池，与调用级并发池分开，避免嵌套提交死锁
_TIMEOUT_POOL = ThreadPoolExecutor(max_workers=16, thread_name_prefix="tool-timeout")


def _compact(error: ValidationError) -> str:
    parts = []
    for item in error.errors()[:5]:
        location = ".".join(str(part) for part in item["loc"]) or "(root)"
        parts.append(f"{location}: {item['msg']}")
    return "; ".join(parts)


class ToolExecutor:
    """按 ``ToolSpec`` 执行工具，统一错误与超时语义。"""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        max_workers: int = 4,
        tracer: Any = None,
    ) -> None:
        if max_workers < 1:
            raise ValueError("max_workers 至少为 1")
        self.registry = registry
        self.max_workers = max_workers
        self.tracer = tracer
        self._pool: ThreadPoolExecutor | None = None

    # -- 单次执行 ---------------------------------------------------------
    def execute(
        self, call: ToolCall, *, defaults: Mapping[str, Any] | None = None
    ) -> ToolResult:
        """``defaults`` 提供**会话级环境参数**（如 ``user_id``）。

        显式参数优先于 defaults：合并顺序为 ``defaults`` → ``call.arguments``。
        这样 Plan 里的 ``get_calendar_events`` 无需让模型凭空编出 ``user_id``。
        """

        spec = self.registry.get(call.tool_name)
        if spec is None:
            return self._failure(call, "unknown_tool", f"未注册的工具：{call.tool_name}")

        arguments: dict[str, Any] = dict(defaults or {})
        arguments.update(call.arguments)

        try:
            payload = spec.input_model.model_validate(arguments)
        except ValidationError as exc:
            return self._failure(call, "invalid_arguments", _compact(exc))

        started = time.perf_counter()
        attempts = 0
        max_attempts = 1 if spec.side_effect else spec.max_retries + 1
        raw: Any = None
        error_code = "tool_error"
        error_message = ""
        succeeded = False

        while attempts < max_attempts:
            attempts += 1
            try:
                raw = self._invoke(spec, payload)
            except FutureTimeout:
                error_code, error_message = "timeout", f"工具执行超时（>{spec.timeout_s}s）"
            except ProviderError as exc:
                error_code, error_message = exc.code, str(exc)
            except Exception as exc:  # noqa: BLE001 - 工具内部任何异常都要变成结果
                error_code, error_message = "tool_error", f"{type(exc).__name__}: {exc}"
            else:
                succeeded = True
                break
            if error_code in _NON_RETRYABLE:
                break

        latency_ms = int((time.perf_counter() - started) * 1000)

        if not succeeded:
            self._trace(
                "tool_result",
                tool=spec.name,
                ok=False,
                error_code=error_code,
                attempts=attempts,
                latency_ms=latency_ms,
            )
            return ToolResult(
                call_id=call.call_id,
                ok=False,
                tool_name=spec.name,
                error_code=error_code,
                error_message=error_message,
                latency_ms=latency_ms,
            )

        try:
            data = spec.output_model.model_validate(raw).model_dump()
        except ValidationError as exc:
            self._trace(
                "tool_result",
                tool=spec.name,
                ok=False,
                error_code="invalid_output",
                attempts=attempts,
                latency_ms=latency_ms,
            )
            return ToolResult(
                call_id=call.call_id,
                ok=False,
                tool_name=spec.name,
                error_code="invalid_output",
                error_message=f"工具输出不符合 {spec.output_model.__name__}：{_compact(exc)}",
                latency_ms=latency_ms,
            )

        self._trace(
            "tool_result", tool=spec.name, ok=True, attempts=attempts, latency_ms=latency_ms
        )
        return ToolResult(
            call_id=call.call_id, ok=True, tool_name=spec.name, data=data, latency_ms=latency_ms
        )

    # -- 并发执行 ---------------------------------------------------------
    def execute_all(
        self, calls: Sequence[ToolCall], *, defaults: Mapping[str, Any] | None = None
    ) -> list[ToolResult]:
        """有界并发执行一批调用，**保持输入顺序**返回结果。"""

        if len(calls) <= 1:
            return [self.execute(call, defaults=defaults) for call in calls]
        runner = partial(self.execute, defaults=defaults)
        return list(self._get_pool().map(runner, calls))

    # -- 生命周期 ---------------------------------------------------------
    def close(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False)
            self._pool = None

    def __enter__(self) -> "ToolExecutor":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- 内部 -------------------------------------------------------------
    def _get_pool(self) -> ThreadPoolExecutor:
        if self._pool is None:
            self._pool = ThreadPoolExecutor(
                max_workers=self.max_workers, thread_name_prefix="tool-call"
            )
        return self._pool

    def _invoke(self, spec: Any, payload: Any) -> Any:
        """在独立池中执行 handler，以便强制 ``timeout_s``。"""

        self._trace("tool_call", tool=spec.name, arguments=payload.model_dump())
        future = _TIMEOUT_POOL.submit(spec.handler, payload)
        return future.result(timeout=spec.timeout_s)

    def _failure(self, call: ToolCall, code: str, message: str) -> ToolResult:
        self._trace("tool_result", tool=call.tool_name, ok=False, error_code=code)
        return ToolResult(
            call_id=call.call_id,
            ok=False,
            tool_name=call.tool_name,
            error_code=code,
            error_message=message,
        )

    def _trace(self, kind: str, **payload: Any) -> None:
        if self.tracer is not None:
            self.tracer.log(kind, **payload)
