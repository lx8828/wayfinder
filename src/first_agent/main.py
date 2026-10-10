"""v2 命令行入口：``chat`` / ``eval`` / ``memory`` / ``trace``。

CLI 只负责参数解析与打印；数据读取与统计在 ``core.inspect``，
装配与业务在 ``core.engine`` / ``runtime``，以保证这些能力都可被单元测试直接调用。
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Sequence

from first_agent import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="first-agent", description="Tool Agent Runtime (v2)")
    parser.add_argument("--version", action="store_true", help="打印版本并退出")
    subparsers = parser.add_subparsers(dest="command")

    chat = subparsers.add_parser("chat", help="单轮对话（--mode react / plan）")
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

    memory = subparsers.add_parser("memory", help="查看 / 管理长期记忆")
    memory.add_argument("action", choices=["list", "show", "rm"], help="list / show / rm")
    memory.add_argument("key", nargs="?", help="show 与 rm 需要指定 key")
    memory.add_argument("--user", default="demo", help="用户 ID（默认 demo）")
    memory.add_argument("--all-users", action="store_true", help="list 时列出全部用户")
    memory.add_argument("--json", action="store_true", help="以 JSON 输出")

    trace = subparsers.add_parser("trace", help="查看 Trace 记录与统计")
    trace.add_argument("action", nargs="?", default="stats", choices=["tail", "stats"])
    trace.add_argument("--n", type=int, default=20, help="tail 显示条数（默认 20）")
    trace.add_argument("--dir", default=None, help="Trace 目录（默认 var/traces）")
    trace.add_argument("--json", action="store_true", help="以 JSON 输出")
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
    if args.command == "memory":
        return _run_memory(args)
    if args.command == "trace":
        return _run_trace(args)

    print(f"[first-agent] 未知子命令：{args.command!r}", file=sys.stderr)
    return 2


def _run_chat(args: argparse.Namespace) -> int:
    # 延迟导入：让 --version / --help 不依赖 openai / httpx / dotenv
    from pathlib import Path

    from dotenv import load_dotenv
    from openai import OpenAI

    from first_agent.core.client import LLMClient
    from first_agent.core.context import SUMMARY_TRIGGER_TOKENS, SUMMARY_TRIGGER_TURNS
    from first_agent.core.db import Database
    from first_agent.core.engine import ToolAgentRuntime
    from first_agent.core.session import SessionStore
    from first_agent.core.tracing import TraceLogger
    from first_agent.memory.service import MemoryService
    from first_agent.memory.store import MemoryStore
    from first_agent.memory.summarizer import SessionSummarizer
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

    tracer = TraceLogger(
        directory=os.getenv("FIRST_AGENT_TRACE_DIR", "var/traces"),
        mode=os.getenv("TRACE_MODE", "safe"),
        enabled=not args.no_trace,
    )
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

    # 摘要阈值可配置（design 10.1「阈值必须配置化」）
    summary_trigger_turns = int(os.getenv("SUMMARY_TRIGGER_TURNS", SUMMARY_TRIGGER_TURNS))
    summary_trigger_tokens = int(os.getenv("SUMMARY_TRIGGER_TOKENS", SUMMARY_TRIGGER_TOKENS))

    with Database(db_path) as db:
        sessions = SessionStore(db)
        memories = MemoryStore(db)
        runtime = ToolAgentRuntime(
            engine=ReactEngine(client, registry, tracer=tracer),
            plan_engine=PlanExecuteEngine(client, registry, tracer=tracer),
            sessions=sessions,
            memories=memories,
            memory_service=MemoryService(client, memories, tracer=tracer),
            summarizer=SessionSummarizer(
                client,
                sessions,
                trigger_turns=summary_trigger_turns,
                trigger_tokens=summary_trigger_tokens,
                tracer=tracer,
            ),
            # 窗口策略必须与摘要触发阈值一致，否则会出现「既不摘要也不保留」的缺口
            summary_trigger_turns=summary_trigger_turns,
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

    if args.show_memory and (outcome.memory_actions or outcome.summary_action):
        print("--- Memory ---")
        for key, action in outcome.memory_actions.items():
            print(f"  {key}: {action}")
        if outcome.summary_action and outcome.summary_action != "nothing_to_do":
            covered = outcome.summary_covered_through
            print(f"  短期摘要: {outcome.summary_action}（覆盖到第 {covered} 轮）")
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
        summary_action=outcome.summary_action,
        summary_covered_through=outcome.summary_covered_through,
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


def _run_memory(args: argparse.Namespace) -> int:
    """查看 / 管理长期记忆（只读为主，rm 是显式删除）。"""

    import json
    from pathlib import Path

    from first_agent.core import inspect as inspect_mod
    from first_agent.core.db import Database
    from first_agent.memory.store import MemoryStore

    if args.action in ("show", "rm") and not args.key:
        print(f"[first-agent] memory {args.action} 需要指定 key。", file=sys.stderr)
        return 2

    db_path = Path(os.getenv("FIRST_AGENT_DB", "var/agent.db"))
    if args.action == "list" and not db_path.exists():
        print(f"[first-agent] 数据库不存在：{db_path}（还没有任何记忆）")
        return 0
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with Database(db_path) as db:
        store = MemoryStore(db)

        if args.action == "list":
            by_user = inspect_mod.list_memories(
                store, None if args.all_users else args.user
            )
            print(
                inspect_mod.memory_as_json(by_user)
                if args.json
                else inspect_mod.format_memories(by_user)
            )
            return 0

        if args.action == "show":
            record = store.get(args.user, args.key)
            if record is None:
                print(f"[first-agent] 没有记忆：{args.user}/{args.key}", file=sys.stderr)
                return 1
            if args.json:
                print(
                    json.dumps(
                        {
                            "user_id": record.user_id,
                            "key": record.key,
                            "value": record.value,
                            "category": record.category,
                            "source": record.source,
                            "updated_at": record.updated_at,
                            "last_seen_at": record.last_seen_at,
                        },
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                print(
                    f"{record.key} = {record.value}"
                    f"（category={record.category}，来源={record.source}，"
                    f"最近命中={record.last_seen_at}）"
                )
            return 0

        removed = store.delete(args.user, args.key)
        print(f"[first-agent] {'已删除' if removed else '不存在'}：{args.user}/{args.key}")
        return 0 if removed else 1


def _run_trace(args: argparse.Namespace) -> int:
    """查看 Trace 最近事件与聚合统计（Trace 在写入时已按 TRACE_MODE 脱敏）。"""

    import json
    from pathlib import Path

    from first_agent.core import inspect as inspect_mod

    directory = Path(args.dir or os.getenv("FIRST_AGENT_TRACE_DIR", "var/traces"))
    limit = args.n if args.action == "tail" else 5000
    read = inspect_mod.read_trace_events(directory, limit=limit)

    if not read.events:
        print(f"[first-agent] 没有找到 Trace 记录：{directory}", file=sys.stderr)
        return 0

    if args.action == "tail":
        print(
            json.dumps(read.events, ensure_ascii=False, indent=2)
            if args.json
            else inspect_mod.format_events(read.events, limit=args.n)
        )
    else:
        stats = inspect_mod.trace_stats(read.events)
        print(
            json.dumps(stats, ensure_ascii=False, indent=2, sort_keys=True)
            if args.json
            else inspect_mod.format_stats(stats)
        )

    if read.malformed:
        print(f"[first-agent] 跳过 {read.malformed} 条无法解析的记录", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
