"""CLI 参数解析与未实现子命令的行为测试（不发起网络请求）。"""

from __future__ import annotations

from first_agent.main import build_parser, main


def test_version_flag(capsys):
    assert main(["--version"]) == 0
    assert "first-agent" in capsys.readouterr().out


def test_no_command_prints_help(capsys):
    assert main([]) == 0
    assert "usage" in capsys.readouterr().out


def test_plan_mode_is_reported_as_pending(capsys):
    assert main(["chat", "北京天气", "--mode", "plan"]) == 2
    assert "S6" in capsys.readouterr().err


def test_pending_subcommands_report_phase(capsys):
    for name in ("memory", "trace", "eval"):
        assert main([name]) == 2
        assert "尚未实现" in capsys.readouterr().err


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
