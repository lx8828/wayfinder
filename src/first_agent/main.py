"""v2 命令行入口。

当前阶段（S1）只提供 ``--version`` 与子命令占位；
``chat`` / ``memory`` / ``trace`` / ``eval`` 分别在 design.md 里程碑
Phase 1～4 中实现。
"""

from __future__ import annotations

import argparse
import sys
from typing import Sequence

from first_agent import __version__

_PENDING = {
    "chat": "Phase 1",
    "memory": "Phase 2",
    "trace": "Phase 4",
    "eval": "Phase 4",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="first-agent", description="Tool Agent Runtime (v2)")
    parser.add_argument("--version", action="store_true", help="打印版本并退出")
    subparsers = parser.add_subparsers(dest="command")
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

    print(
        f"[first-agent] 子命令 {args.command!r} 尚未实现，计划于 {_PENDING[args.command]} 提供。",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
