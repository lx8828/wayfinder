"""v2 命令行入口。

已实现：``chat``（S3 react / S6 plan）、``eval``（S7 评测与回归门禁）
未实现：``memory`` / ``trace``（S8）
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Sequence

from first_agent import __version__

_PENDING = {
    "memory": "S8",
    "trace": "S8",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="first-agent", description="Tool Agent Runtime (v2)")
    parser.add_argument("--version", action="store_true", help="打印版本并退出")
    subparsers = parser.add_subparsers(dest="command")

    chat = subparsers.add_parser("chat", help="单轮对话（ReAct）")
    chat.add_argument("message", help="用户输入")
    chat.add_argument("--mode", default="react", choices=["react", "plan"], help="对话模式")
    chat.add_argument("--user", default="demo", help="用户 ID（用于 Trace）")
    chat.add_argument("--session", default="default", help="会话 ID（S4 起用于会话持久化）")
    chat.add_argument("--show-thought", action="store_true", help="在回答前打印 Thought")
    chat.add_argument("--show-memory", action="store_true", help="在回答后打印本轮记忆动作")
    chat.add_argument(
        "--budget-ms", type=int, default=60_000, help="单次 run 耗时预算（毫秒，默认 60000）"
    )
    chat.add_argument("--no-trace", action="store_true", help="关闭 Trace 写入")

    evals = subparsers.add_parser("eval", help="运行评测用例并做 baseline 回归门禁")
    evals.add_argument("--real", action="store_true", help="用真实 LLM 跑（波动指标，需要 API Key）")
    evals.add_argument("--cases", default=None, help="用例文件（默认 evals/cases.jsonl）")
    evals.add_argument("--out", default=None, help="产物目录（默认 evals/）")
    evals.add_argument(
        "--update-baseline", action="store_true", help="把本次结果写为基线后退出"
    )
    evals.add_argument("--no-gate", action="store_true", help="只报告，不因门禁失败返回非零")
    evals.add_argument(
        "--judge", action="store_true", help="额外跑 judge_cases.jsonl（开放式答案，需配合 --real）"
    )
    evals.add_argument("--quiet", action="store_true", help="只输出门禁结论与失败用例")

    for name, phase in _PENDING.items():
        subparsers.add_parser(name, help=f"未实现（{phase}）")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"first-agent {__version__}")
        return 0
    if args.command is None:
        parser.print_help()
        return 0
    if args.command == "chat":
        return _run_chat(args)
    if args.command == "eval":
        return _run_eval(args)

    print(
        f"[first-agent] 子命令 {args.command!r} 尚未实现，计划于 {_PENDING[args.command]} 提供。",
        file=sys.stderr,
    )
    return 2


def _run_chat(args: argparse.Namespace) -> int:
    # 延迟导入：让 --version / --help 不依赖 openai / httpx / dotenv
    from pathlib import Path

    from dotenv import load_dotenv
    from openai import OpenAI

    from first_agent.core.client import LLMClient
    from first_agent.core.db import Database
    from first_agent.core.engine import ToolAgentRuntime
    from first_agent.core.session import SessionStore
    from first_agent.core.tracing import TraceLogger
    from first_agent.memory.service import MemoryService
    from first_agent.memory.store import MemoryStore
    from first_agent.providers import (
        HeuristicTravelProvider,
        LlmDateTimeProvider,
        LocalCalendarProvider,
        QWeatherProvider,
    )
    from first_agent.runtime import PlanExecuteEngine, ReactEngine
    from first_agent.tools.builtin import (
        make_calendar_tool,
        make_resolve_datetime_tool,
        make_time_tool,
        make_travel_time_tool,
        make_weather_tool,
    )
    from first_agent.tools.registry import ToolRegistry

    load_dotenv()

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print("[first-agent] 缺少 DEEPSEEK_API_KEY（可在 .env 中配置）。", file=sys.stderr)
        return 2

    tracer = TraceLogger(enabled=not args.no_trace)
    sdk = OpenAI(
        api_key=api_key,
        base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
    )
    client = LLMClient(sdk, model=os.getenv("DEEPSEEK_MODEL", "deepseek-chat"), tracer=tracer)

    registry = ToolRegistry()
    registry.register(make_time_tool())
    registry.register(make_resolve_datetime_tool(LlmDateTimeProvider(client, tracer=tracer)))
    registry.register(make_travel_time_tool(HeuristicTravelProvider()))
    registry.register(make_calendar_tool(LocalCalendarProvider(os.getenv("CALENDAR_PATH", "var/calendar.json"))))
    if os.getenv("QWEATHER_API_KEY"):
        registry.register(
            make_weather_tool(
                QWeatherProvider(
                    api_key=os.environ["QWEATHER_API_KEY"], host=os.getenv("QWEATHER_HOST")
                )
            )
        )
    else:
        print("[first-agent] 未配置 QWEATHER_API_KEY，本轮不注册 get_weather。", file=sys.stderr)

    db_path = Path(os.getenv("FIRST_AGENT_DB", "var/agent.db"))
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with Database(db_path) as db:
        memories = MemoryStore(db)
        runtime = ToolAgentRuntime(
            engine=ReactEngine(client, registry, tracer=tracer),
            plan_engine=PlanExecuteEngine(client, registry, tracer=tracer),
            sessions=SessionStore(db),
            memories=memories,
            memory_service=MemoryService(client, memories, tracer=tracer),
            budget_ms=args.budget_ms,
            tracer=tracer,
        )
        tracer.start_run(
            user_id=args.user, session_id=args.session, mode=args.mode, mode_selection=args.mode
        )
        outcome = runtime.chat(
            args.message, user_id=args.user, session_id=args.session, mode=args.mode
        )

    result = outcome.result
    # 统一读取：PlanResult 与 ReactResult 都暴露 thoughts（降级到 ReAct 时同样有值）
    thoughts = list(getattr(result, "thoughts", None) or [])
    if args.show_thought and thoughts:
        print("--- Thought ---")
        for index, thought in enumerate(thoughts, 1):
            print(f"[{index}] {thought}")
        print("---------------")

    print(result.answer)

    if args.show_memory and outcome.memory_actions:
        print("--- Memory ---")
        for key, action in outcome.memory_actions.items():
            print(f"  {key}: {action}")
        print("--------------")

    tracer.end_run(
        status=result.status,
        latency_ms=result.latency_ms,
        final_answer=result.answer,
        steps=result.steps,
        tool_calls=result.tool_calls,
        memory_actions=outcome.memory_actions,
        budget_spent_ms=outcome.budget_spent_ms,
        degraded_to=getattr(result, "degraded_to", None),
    )
    # need_info（追问）是正常结果，不是错误
    return 0 if result.status in {"success", "need_info"} else 1


def _run_eval(args: argparse.Namespace) -> int:
    """离线/真 LLM 评测 + baseline 回归门禁（design.md 16.4 / 16.5）。"""

    from pathlib import Path

    from first_agent.evals import offline, suite

    cases_path = Path(args.cases) if args.cases else Path(__file__).resolve().parent / "evals" / "cases.jsonl"
    cases = suite.load_cases_file(cases_path)
    if not cases:
        print(f"[first-agent] 用例文件为空：{cases_path}", file=sys.stderr)
        return 2
    out_dir = Path(args.out) if args.out else cases_path.parent
    client = None

    if args.real:
        from first_agent.evals import live

        try:
            client = live.build_live_client()
        except RuntimeError as exc:
            print(f"[first-agent] {exc}", file=sys.stderr)
            return 2
        factory = live.live_engine_factory(client=client)
        mode, model, suffix = "real", os.getenv("DEEPSEEK_MODEL", live.DEFAULT_MODEL), "_live"
    else:
        if args.judge:
            print("[first-agent] --judge 需要与 --real 一起使用（裁判必须调真实模型）。", file=sys.stderr)
            return 2
        factory = offline.offline_engine_factory
        mode, model, suffix = "offline", "scripted+fake", ""

    metadata = suite.build_metadata(
        cases_path=cases_path,
        mode=mode,
        model=model,
        registry=offline.build_registry(cases[0]),
    )
    run = suite.run_suite(cases, factory, metadata=metadata)

    if not args.quiet:
        print(f"[first-agent] 评测完成：{len(run.checks)} 条用例（mode={mode}，model={model}）")
        print(suite.metrics_report(run.metrics))

    if args.judge:
        from first_agent.evals import judge as judge_mod

        judge_cases = judge_mod.load_judge_cases(cases_path.parent / "judge_cases.jsonl")
        judged = judge_mod.run_judge_cases(
            judge_cases, factory, judge_mod.LlmJudge(client)
        )
        rate = judge_mod.task_success_rate(judged)
        if rate is not None:
            run.metrics["Task Success Rate"] = rate
        if not args.quiet:
            print(judge_mod.format_judge_report(judged))
        print(f"[first-agent] judge 样本 {len(judged)} 条，Task Success Rate = {rate}")

    baseline_path = out_dir / f"baseline{suffix}.json"
    if args.update_baseline:
        if run.failures:
            print(suite.failure_report(run.checks), file=sys.stderr)
            print(
                f"[first-agent] 有 {len(run.failures)} 条用例未通过，拒绝写为基线。",
                file=sys.stderr,
            )
            return 1
        suite.save_baseline(baseline_path, run)
        paths = suite.write_outputs(run, out_dir=out_dir, suffix=suffix)
        print(f"[first-agent] 基线已更新：{baseline_path}")
        print(f"[first-agent] 当前结果：{paths['current']}")
        return 0

    gate = suite.evaluate_gate(suite.load_baseline(baseline_path), run.metrics)
    paths = suite.write_outputs(run, out_dir=out_dir, suffix=suffix, gate=gate)
    print(f"[first-agent] 产物：{paths['current']} / {paths.get('diff', '—')}")
    print(suite.failure_report(run.checks))
    if not args.quiet:
        print(gate.diff_markdown)

    if gate.failed_deltas:
        names = "、".join(delta.name for delta in gate.failed_deltas)
        print(f"[first-agent] 门禁失败：稳定指标恶化 —— {names}", file=sys.stderr)
        return 0 if args.no_gate else 1
    if gate.warnings:
        names = "、".join(delta.name for delta in gate.warnings)
        print(f"[first-agent] 警告（波动指标恶化，需人工复核）：{names}", file=sys.stderr)
    if run.failures:
        print(f"[first-agent] 门禁失败：{len(run.failures)} 条用例未通过", file=sys.stderr)
        return 0 if args.no_gate else 1

    print("[first-agent] 门禁通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
