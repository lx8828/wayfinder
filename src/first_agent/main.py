"""v2 命令行入口。

已实现：``chat --mode react``（S3）
未实现：``memory``（S4）、``chat --mode plan``（S6）、``trace`` / ``eval``（S7）
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Sequence

from first_agent import __version__

_PENDING = {
    "memory": "S4",
    "trace": "S7",
    "eval": "S7",
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


if __name__ == "__main__":
    raise SystemExit(main())
