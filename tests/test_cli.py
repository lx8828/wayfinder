"""CLI 参数解析与子命令行为测试（不发起网络请求）。"""

from __future__ import annotations

import argparse

import pytest

from first_agent.main import build_parser, main


def test_version_flag(capsys):
    assert main(["--version"]) == 0
    assert "first-agent" in capsys.readouterr().out


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    assert "usage" in capsys.readouterr().out


def test_plan_mode_is_accepted_by_parser():
    args = build_parser().parse_args(["chat", "北京天气", "--mode", "plan"])

    assert args.mode == "plan"


def test_unknown_mode_is_rejected_by_parser(capsys):
    with pytest.raises(SystemExit):
        build_parser().parse_args(["chat", "北京天气", "--mode", "nope"])


def test_cli_surface_is_complete():
    """Phase 5 收口后不应再存在「尚未实现」的子命令。"""

    parser = build_parser()
    subparsers = next(
        action
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )

    assert set(subparsers.choices) == {"chat", "eval", "memory", "trace"}


def test_memory_requires_an_action():
    with pytest.raises(SystemExit) as excinfo:
        main(["memory"])

    assert excinfo.value.code == 2


def test_eval_subcommand_runs_offline(tmp_path, capsys):
    """`eval` 在无 API Key 下可跑完整套件，产物写到 --out 指定的目录。"""

    assert main(["eval", "--quiet", "--out", str(tmp_path)]) == 0

    capsys.readouterr()
    assert (tmp_path / "current.json").exists()
    assert (tmp_path / "diff.md").exists()


def test_chat_parser_accepts_expected_flags():
    args = build_parser().parse_args(
        ["chat", "你好", "--user", "u1", "--session", "s1", "--show-thought", "--no-trace"]
    )

    assert args.command == "chat"
    assert args.message == "你好"
    assert args.user == "u1"
    assert args.session == "s1"
    assert args.show_thought is True
    assert args.no_trace is True
    assert args.mode == "react"
