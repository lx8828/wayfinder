"""Plan-Execute 引擎。对应 design.md 第 8 节。

流程（8.2）：

    读取 Working State → 合并长期记忆默认值（由 ContextBuilder 注入 system 上下文）
    → 抽取本轮槽位 → 生成 Plan → Schema/引用/DAG 校验 → 拓扑排序
    → 逐步执行（解析 ``$steps.<id>.<字段>``）→ 写回 Working State → 汇总回答

与 ``ReactEngine`` 保持同一入口签名 ``run_messages(messages, *, working_state, defaults)``，
因此 ``ToolAgentRuntime`` 可以按 mode 直接切换引擎。

注意：``working_state`` 是**原地更新**的（供上层持久化），这是与上层约定的契约。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from first_agent.core.budget import (
    DEFAULT_BUDGET_MS,
    BudgetExceeded,
    RunBudget,
    build_partial_answer,
    check_budget,
)
from first_agent.core.client import JSONValidationError, LLMClient, LLMError
from first_agent.core.context import DEFAULT_SYSTEM_PROMPT
from first_agent.runtime.slots import SlotExtractor
from first_agent.schemas.plan import Plan, PlanStep, iter_references, parse_step_reference
from first_agent.schemas.session import SLOT_FIELDS, WorkingState
from first_agent.schemas.tool import ToolCall, ToolResult
from first_agent.tools.executor import ToolExecutor
from first_agent.tools.registry import ToolRegistry

DEFAULT_MAX_STEPS = 5


class PlanExecutionError(RuntimeError):
    """引用无法解析等执行期错误。"""


# -- 计划校验 --------------------------------------------------------------


@dataclass(frozen=True)
class PlanProblem:
    kind: str
    detail: str


def _detect_cycles(plan: Plan) -> list[PlanProblem]:
    graph = {
        step.id: [item for item in step.depends_on if item != step.id] for step in plan.steps
    }
    visiting: set[int] = set()
    done: set[int] = set()
    problems: list[PlanProblem] = []

    def visit(node: int, path: list[int]) -> None:
        if node in done:
            return
        if node in visiting:
            cycle = " → ".join(str(item) for item in [*path, node])
            problems.append(PlanProblem("cycle", f"依赖成环：{cycle}"))
            return
        visiting.add(node)
        for neighbour in graph.get(node, []):
            visit(neighbour, [*path, node])
        visiting.discard(node)
        done.add(node)

    for node in graph:
        visit(node, [])
    return problems


def validate_plan(plan: Plan, registry: ToolRegistry, *, max_steps: int) -> list[PlanProblem]:
    """Schema / 重复 id / 工具存在性 / 依赖存在性 / 引用三步 / 环检测。"""

    problems: list[PlanProblem] = []

    for step_id in plan.duplicate_ids():
        problems.append(PlanProblem("duplicate_id", f"step id 重复：{step_id}"))

    if len(plan.steps) > max_steps:
        problems.append(
            PlanProblem("too_many_steps", f"{len(plan.steps)} 步超过上限 {max_steps}")
        )

    known = plan.step_ids()
    by_id = {step.id: step for step in plan.steps}

    for step in plan.steps:
        spec = registry.get(step.tool)
        if spec is None:
            problems.append(
                PlanProblem("unknown_tool", f"step {step.id}: 未注册的工具 {step.tool}")
            )

        for dependency in step.depends_on:
            if dependency == step.id:
                problems.append(PlanProblem("cycle", f"step {step.id} 依赖自身"))
            elif dependency not in known:
                problems.append(
                    PlanProblem(
                        "missing_dependency",
                        f"step {step.id} 依赖了不存在的 step {dependency}",
                    )
                )

        try:
            references = list(iter_references(step.arguments))
        except ValueError as exc:
            problems.append(PlanProblem("bad_reference", f"step {step.id}: {exc}"))
            continue

        for referenced_id, field_name in references:
            if referenced_id not in known:
                problems.append(
                    PlanProblem(
                        "bad_reference",
                        f"step {step.id} 引用了不存在的 step {referenced_id}",
                    )
                )
                continue
            if referenced_id not in step.depends_on:
                problems.append(
                    PlanProblem(
                        "missing_dependency",
                        f"step {step.id} 引用了 step {referenced_id}，但没有声明 depends_on",
                    )
                )
            source_spec = registry.get(by_id[referenced_id].tool)
            if source_spec is not None and field_name not in source_spec.output_model.model_fields:
                problems.append(
                    PlanProblem(
                        "unknown_field",
                        f"step {step.id} 引用了 {source_spec.name} 上不存在的字段 {field_name}",
                    )
                )

    problems.extend(_detect_cycles(plan))

    # 去重并保持顺序（环检测可能重复报告）
    unique: dict[tuple[str, str], PlanProblem] = {}
    for problem in problems:
        unique.setdefault((problem.kind, problem.detail), problem)
    return list(unique.values())


def topological_order(plan: Plan) -> list[PlanStep]:
    """按 ``depends_on`` 拓扑排序；同层按 id 升序以保证确定性。"""

    by_id = {step.id: step for step in plan.steps}
    pending: dict[int, set[int]] = {
        step.id: {item for item in step.depends_on if item in by_id and item != step.id}
        for step in plan.steps
    }

    ordered: list[PlanStep] = []
    while pending:
        ready = sorted(node for node, deps in pending.items() if not deps)
        if not ready:
            raise PlanExecutionError(f"依赖成环，无法拓扑排序：{sorted(pending)}")
        for node in ready:
            ordered.append(by_id[node])
            pending.pop(node)
        for deps in pending.values():
            deps.difference_update(ready)
    return ordered


def resolve_arguments(step: PlanStep, outputs: Mapping[int, dict[str, Any]]) -> dict[str, Any]:
    """把 ``$steps.<id>.<字段>`` 替换为前序步骤的实际输出。"""

    def resolve(node: Any) -> Any:
        if isinstance(node, dict):
            return {key: resolve(value) for key, value in node.items()}
        if isinstance(node, list):
            return [resolve(item) for item in node]
        reference = parse_step_reference(node)
        if reference is None:
            return node
        source_id, field_name = reference
        data = outputs.get(source_id)
        if data is None:
            raise PlanExecutionError(f"step {step.id}: 引用的 step {source_id} 尚无结果")
        if field_name not in data:
            raise PlanExecutionError(
                f"step {step.id}: step {source_id} 的结果没有字段 {field_name}"
            )
        return data[field_name]

    return {key: resolve(value) for key, value in step.arguments.items()}


# -- 结果 ------------------------------------------------------------------


@dataclass
class PlanResult:
    answer: str = ""
    plan: Plan | None = None
    # steps 是**步数**（与 ReactResult 对齐，供评测适配器直接转换）
    steps: int = 0
    step_traces: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[str] = field(default_factory=list)
    arguments: dict[str, list[dict]] = field(default_factory=dict)
    tool_results: list[ToolResult] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    working_state: WorkingState | None = None
    latency_ms: int = 0
    # success / need_info / invalid_plan / budget_exceeded / error
    status: str = "success"
    error: str | None = None
    need_info: list[str] = field(default_factory=list)
    plan_problems: list[PlanProblem] = field(default_factory=list)
    # 降级到 ReAct 时保留其 Thought（CLI/`--show-thought` 统一读本属性）
    thoughts: list[str] = field(default_factory=list)
    degraded_to: str | None = None


# -- 引擎 ------------------------------------------------------------------

PLANNER_TEMPLATE = """你是一个任务规划器，把用户的出行请求拆解为可执行的工具调用步骤。

可用工具：
{tools}

规则：
1. 只返回 JSON：{{"need_info": [], "reason": "", "steps": []}}
2. 缺少完成任务必需的信息时（例如不知道目的地），把缺失字段放进 need_info，steps 留空。
3. 每一步形如 {{"id": 0, "tool": "工具名", "arguments": {{}}, "depends_on": []}}。
4. 参数需要引用前面步骤的结果时写 "$steps.<id>.<字段>"，并且**必须**把该 id 写进 depends_on。
5. id 从 0 开始、不能重复；步骤总数不超过 {max_steps}。
6. 不要编造用户没说过的城市或日期；能用「已知用户信息」的城市就优先用它。

示例（已知居住地重庆，用户说明天下午去公园）：
{{"need_info": [], "reason": "先解析时间，再查对应日期天气",
 "steps": [
   {{"id": 0, "tool": "resolve_datetime", "arguments": {{"expression": "明天下午"}}, "depends_on": []}},
   {{"id": 1, "tool": "get_weather", "arguments": {{"city": "重庆", "date": "$steps.0.date"}}, "depends_on": [0]}}
 ]}}
"""


def _tool_catalog(registry: ToolRegistry) -> str:
    lines: list[str] = []
    for schema in registry.schemas():
        function = schema["function"]
        properties = function["parameters"].get("properties", {})
        arguments = ", ".join(properties) if properties else "无参数"
        lines.append(f"- {function['name']}({arguments})：{function['description']}")
    return "\n".join(lines) if lines else "（当前没有可用工具）"


def _last_user_message(messages: Sequence[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _system_context(messages: Sequence[dict]) -> str:
    for message in messages:
        if message.get("role") == "system":
            return str(message.get("content") or "")
    return ""


def _observations(step_traces, tool_results) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for trace, result in zip(step_traces, tool_results):
        items.append(
            {
                "step": trace["id"],
                "tool": trace["tool"],
                "ok": result.ok,
                "data": result.data,
                "error_code": result.error_code,
                "error_message": result.error_message,
            }
        )
    return items


def _fallback_summary(items: Sequence[dict[str, Any]]) -> str:
    lines = ["这是我目前掌握的信息："]
    for item in items:
        if item["ok"]:
            lines.append(f"- {item['tool']}：{json.dumps(item['data'], ensure_ascii=False)}")
        else:
            lines.append(
                f"- {item['tool']} 失败（{item['error_code']}）：{item['error_message']}"
            )
    return "\n".join(lines)


class PlanExecuteEngine:
    def __init__(
        self,
        client: LLMClient,
        registry: ToolRegistry,
        *,
        executor: ToolExecutor | None = None,
        slot_extractor: SlotExtractor | None = None,
        system_prompt: str | None = None,
        max_steps: int = DEFAULT_MAX_STEPS,
        fallback_engine: Any = None,
        budget_ms: int = DEFAULT_BUDGET_MS,
        clock: Callable[[], float] | None = None,
        tracer: Any = None,
    ) -> None:
        if max_steps < 1:
            raise ValueError("max_steps 至少为 1")
        self.client = client
        self.registry = registry
        self.executor = executor or ToolExecutor(registry, tracer=tracer)
        self.slot_extractor = slot_extractor or SlotExtractor(client, tracer=tracer)
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
        self.max_steps = max_steps
        # Plan 失败时的降级目标（通常是 ReactEngine）；不注入则退化为追问
        self.fallback_engine = fallback_engine
        self.budget_ms = budget_ms
        self.clock = clock
        self.tracer = tracer
        self.planner_prompt = PLANNER_TEMPLATE.format(
            tools=_tool_catalog(registry), max_steps=max_steps
        )

    # -- 入口 -------------------------------------------------------------
    def run_messages(
        self,
        messages: list[dict],
        *,
        working_state: WorkingState | None = None,
        defaults: Mapping[str, Any] | None = None,
        budget: RunBudget | None = None,
    ) -> PlanResult:
        started = time.perf_counter()
        budget = budget or RunBudget(budget_ms=self.budget_ms, clock=self.clock)
        user_input = _last_user_message(messages)
        state = working_state or WorkingState(user_id="anonymous", session_id="anonymous")
        result = PlanResult(messages=[dict(message) for message in messages], working_state=state)

        # 1) 抽取本轮槽位并合并（8.3：多轮追问时不能清零）
        exceeded = check_budget(budget, "slot_extraction")
        if exceeded is not None:
            return self._degrade_on_budget(result, exceeded, budget, started)
        patch = self.slot_extractor.extract(user_input)
        state.fill(patch.non_empty())

        # 2) 生成计划并校验；不合法则**重规划一次**（design 8.2）
        exceeded = check_budget(budget, "plan")
        if exceeded is not None:
            return self._degrade_on_budget(result, exceeded, budget, started)

        try:
            plan, problems = self._plan_and_validate(state, user_input, messages)
        except JSONValidationError as exc:
            self._trace("plan_generation_failed", error=str(exc))
            result.error = f"规划失败：{exc}"
            return self._handle_plan_failure(result, messages, state, defaults, budget, started)

        result.plan = plan

        # 3) 缺信息 → 追问，并把待补信息写进 Working State
        if plan.need_info:
            state.pending_slots = list(plan.need_info)
            result.status = "need_info"
            result.need_info = list(plan.need_info)
            result.answer = f"我还需要知道{'、'.join(plan.need_info)}，才能帮你判断。"
            return self._finish(result, started)
        state.pending_slots = []

        # 4) 重规划后仍不合法 → 降级到 ReAct 或追问（design 13）
        if problems:
            result.plan_problems = problems
            return self._handle_plan_failure(result, messages, state, defaults, budget, started)

        # 5) 按拓扑序执行
        ambient = {"user_id": state.user_id}
        if defaults:
            ambient.update(defaults)

        outputs: dict[int, dict[str, Any]] = {}
        failed: set[int] = set()

        for index, step in enumerate(topological_order(plan), 1):
            result.steps = index

            exceeded = check_budget(budget, "tool_call")
            if exceeded is not None:
                return self._degrade_on_budget(result, exceeded, budget, started)

            # 依赖失败的步骤**跳过而不是整体崩掉**：其余步骤照常执行、照常汇总
            blocked = sorted(set(step.depends_on) & failed)
            if blocked:
                self._record_step_failure(
                    result, step, "blocked_by_dependency", f"依赖的步骤未成功：{blocked}，本步跳过"
                )
                outputs[step.id] = {}
                failed.add(step.id)
                self._trace("plan_step_blocked", step=step.id, blocked_by=blocked)
                continue

            try:
                arguments = resolve_arguments(step, outputs)
            except PlanExecutionError as exc:
                self._record_step_failure(result, step, "unresolved_reference", str(exc))
                outputs[step.id] = {}
                failed.add(step.id)
                self._trace("plan_reference_failed", step=step.id, error=str(exc))
                continue

            call = ToolCall(call_id=f"step_{step.id}", tool_name=step.tool, arguments=arguments)
            tool_result = self.executor.execute(call, defaults=ambient)
            outputs[step.id] = tool_result.data or {}
            if not tool_result.ok:
                failed.add(step.id)

            result.tool_calls.append(step.tool)
            result.arguments.setdefault(step.tool, []).append(arguments)
            result.tool_results.append(tool_result)
            result.step_traces.append(
                {
                    "id": step.id,
                    "tool": step.tool,
                    "arguments": arguments,
                    "depends_on": list(step.depends_on),
                    "ok": tool_result.ok,
                    "error_code": tool_result.error_code,
                }
            )
            self._trace(
                "plan_step",
                step=step.id,
                tool=step.tool,
                arguments=arguments,
                ok=tool_result.ok,
                error_code=tool_result.error_code,
                latency_ms=tool_result.latency_ms,
            )

        # 6) 汇总回答（汇总也要花一次 LLM 调用，同样受预算约束）
        items = _observations(result.step_traces, result.tool_results)
        exceeded = check_budget(budget, "summary")
        if exceeded is not None:
            # 不能再调 LLM：用确定性兜底文本把已取得的 Observation 交给用户
            self._trace(
                "plan_summary_skipped_budget", spent_ms=exceeded.spent_ms, stage=exceeded.stage
            )
            result.status = "budget_exceeded"
            result.error = str(exceeded)
            result.answer = _fallback_summary(items)
            return self._finish(result, started)

        result.answer = self._summarize(user_input, state, items)
        return self._finish(result, started)

    # -- 内部 -------------------------------------------------------------
    @staticmethod
    def _record_step_failure(
        result: PlanResult, step: PlanStep, error_code: str, message: str
    ) -> None:
        """被跳过/引用失败的步骤：只记录，不计入 ``tool_calls``（工具并未真的被调用）。"""

        result.tool_results.append(
            ToolResult(
                call_id=f"step_{step.id}",
                ok=False,
                tool_name=step.tool,
                error_code=error_code,
                error_message=message,
            )
        )
        result.step_traces.append(
            {
                "id": step.id,
                "tool": step.tool,
                "arguments": dict(step.arguments),
                "depends_on": list(step.depends_on),
                "ok": False,
                "error_code": error_code,
            }
        )

    # -- 规划 -------------------------------------------------------------
    @staticmethod
    def _planner_payload(state: WorkingState, user_input: str, system_context: str) -> dict:
        return {
            "已知用户信息": system_context.strip() or "（无）",
            "任务状态": {
                name: getattr(state, name) for name in SLOT_FIELDS if getattr(state, name)
            },
            "待补充信息": state.pending_slots,
            "用户本轮输入": user_input,
        }

    def _plan(self, state: WorkingState, user_input: str, *, system_context: str) -> Plan:
        messages = [
            {"role": "system", "content": self.planner_prompt},
            {
                "role": "user",
                "content": json.dumps(
                    self._planner_payload(state, user_input, system_context), ensure_ascii=False
                ),
            },
        ]
        return self.client.chat_json(messages, Plan)

    def _plan_and_validate(
        self, state: WorkingState, user_input: str, messages: list[dict]
    ) -> tuple[Plan, list[PlanProblem]]:
        """生成计划并校验；不合法时**只重规划一次**（design.md 8.2）。"""

        context = _system_context(messages)
        plan = self._plan(state, user_input, system_context=context)
        self._trace(
            "plan_generated",
            steps=[step.model_dump() for step in plan.steps],
            need_info=plan.need_info,
            reason=plan.reason,
        )

        problems = validate_plan(plan, self.registry, max_steps=self.max_steps)
        if not problems:
            return plan, []

        self._trace("plan_invalid", attempt=1, problems=[item.detail for item in problems])
        replanned = self._replan(
            state, user_input, context=context, plan=plan, problems=problems
        )
        if replanned is None:
            return plan, problems

        self._trace(
            "plan_regenerated",
            steps=[step.model_dump() for step in replanned.steps],
            need_info=replanned.need_info,
            reason=replanned.reason,
        )
        remaining = validate_plan(replanned, self.registry, max_steps=self.max_steps)
        if remaining:
            self._trace("plan_invalid", attempt=2, problems=[item.detail for item in remaining])
            return replanned, remaining
        return replanned, []

    def _replan(
        self,
        state: WorkingState,
        user_input: str,
        *,
        context: str,
        plan: Plan,
        problems: Sequence[PlanProblem],
    ) -> Plan | None:
        """把校验问题回灌给规划器，**只重试一次**；仍失败返回 ``None``。"""

        payload = self._planner_payload(state, user_input, context)
        payload["上一次计划"] = plan.model_dump()
        payload["校验发现的问题"] = [f"[{item.kind}] {item.detail}" for item in problems]
        payload["要求"] = "请修正上述问题后重新给出完整计划；只返回 JSON，格式与之前一致。"

        messages = [
            {"role": "system", "content": self.planner_prompt},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        try:
            return self.client.chat_json(messages, Plan)
        except JSONValidationError as exc:
            self._trace("plan_replan_failed", error=str(exc))
            return None

    # -- 降级 -------------------------------------------------------------
    def _handle_plan_failure(
        self,
        result: PlanResult,
        messages: list[dict],
        state: WorkingState,
        defaults: Mapping[str, Any] | None,
        budget: RunBudget,
        started: float,
    ) -> PlanResult:
        """拿不到可执行计划时的降级：优先转 ReAct，否则追问用户（design.md 13）。"""

        if self.fallback_engine is not None and check_budget(budget, "fallback_react") is None:
            return self._degrade_to_react(result, messages, state, defaults, budget, started)

        if result.plan_problems:
            result.status = "invalid_plan"
        else:
            result.status = "error"
            result.error = result.error or "规划失败"
        result.answer = "抱歉，我暂时无法为这个请求生成可行的执行计划，能再说明一下你想做什么吗？"
        return self._finish(result, started)

    def _degrade_to_react(
        self,
        result: PlanResult,
        messages: list[dict],
        state: WorkingState,
        defaults: Mapping[str, Any] | None,
        budget: RunBudget,
        started: float,
    ) -> PlanResult:
        """降级到 ReAct，并保留其答案 / 工具轨迹 / Thought。"""

        self._trace(
            "plan_degraded_to_react",
            reason=result.error or "invalid_plan",
            problems=[item.detail for item in result.plan_problems],
        )
        fallback = self.fallback_engine.run_messages(
            messages, working_state=state, defaults=defaults, budget=budget
        )

        result.answer = fallback.answer
        result.tool_calls = list(fallback.tool_calls)
        result.arguments = {key: list(value) for key, value in fallback.arguments.items()}
        result.tool_results = list(fallback.tool_results)
        result.messages = list(fallback.messages)
        result.steps = fallback.steps
        result.status = fallback.status
        # 降级成功就不该留"失败"痕迹，否则评测会把一次成功的降级判成失败
        result.error = fallback.error
        result.thoughts = list(getattr(fallback, "thoughts", []))
        result.degraded_to = "react"
        return self._finish(result, started)

    def _degrade_on_budget(
        self, result: PlanResult, exceeded: BudgetExceeded, budget: RunBudget, started: float
    ) -> PlanResult:
        """预算耗尽的降级：状态 + 部分结果 + Trace（design.md 14.1）。"""

        result.status = "budget_exceeded"
        result.error = str(exceeded)
        result.answer = build_partial_answer(
            result.tool_results,
            spent_ms=exceeded.spent_ms,
            budget_ms=budget.budget_ms,
            stage=exceeded.stage,
        )
        self._trace(
            "run_budget_exceeded",
            spent_ms=exceeded.spent_ms,
            budget_ms=budget.budget_ms,
            stage=exceeded.stage,
        )
        return self._finish(result, started)

    def _summarize(
        self, user_input: str, state: WorkingState, items: Sequence[dict[str, Any]]
    ) -> str:
        slots = {name: getattr(state, name) for name in SLOT_FIELDS if getattr(state, name)}
        messages = [
            {"role": "system", "content": self.system_prompt},
            {
                "role": "user",
                "content": (
                    f"用户问题：{user_input}\n"
                    f"任务状态：{json.dumps(slots, ensure_ascii=False)}\n"
                    f"工具执行结果：{json.dumps(list(items), ensure_ascii=False)}\n"
                    "请基于以上事实用中文简洁回答；若有工具失败，说明情况并给出替代建议；不要编造数据。"
                ),
            },
        ]
        try:
            return self.client.chat_text(messages)
        except LLMError as exc:
            self._trace("plan_summary_failed", error=str(exc))
            return _fallback_summary(items)

    @staticmethod
    def _finish(result: PlanResult, started: float) -> PlanResult:
        result.latency_ms = int((time.perf_counter() - started) * 1000)
        return result

    def _trace(self, kind: str, **payload: Any) -> None:
        if self.tracer is not None:
            self.tracer.log(kind, **payload)
