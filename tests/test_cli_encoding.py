"""CLI 输出编码：重定向到非 UTF-8 编码时不能崩（Windows 实测缺陷）。

复现方式：Windows 下 ``wayfinder eval > out.txt`` 或 ``| Select-String`` 时，
Python 的 stdout 会退化为 ANSI 代码页（简中为 ``gbk``），打印 ``✅``/``✗`` 直接
``UnicodeEncodeError``，命令以 exit 1 结束、输出全空。

这里用 ``PYTHONIOENCODING=gbk`` **跨平台**复现同一路径（比只在 Windows 上跑更可靠）：
先证明"不修就一定崩"，再证明 ``_configure_stdio()`` 兜住了。
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from wayfinder.main import _configure_stdio

SYMBOLS = "门禁通过 ✅ ✗⚠✓"


def _run(script: str, *, encoding: str | None) -> subprocess.CompletedProcess[bytes]:
    env = {key: value for key, value in os.environ.items() if key != "PYTHONUTF8"}
    if encoding is None:
        env.pop("PYTHONIOENCODING", None)
    else:
        env["PYTHONIOENCODING"] = encoding
    return subprocess.run(
        [sys.executable, "-c", script], capture_output=True, env=env, timeout=60
    )


def test_non_utf8_stdout_would_crash_without_the_fix():
    """先钉住缺陷本身：这是回归测试有意义的前提。"""

    completed = _run(f"print({SYMBOLS!r})", encoding="gbk")

    assert completed.returncode != 0
    assert b"UnicodeEncodeError" in completed.stderr


def test_configure_stdio_survives_non_utf8_stdout():
    completed = _run(
        f"from wayfinder.main import _configure_stdio; _configure_stdio(); print({SYMBOLS!r})",
        encoding="gbk",
    )

    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    # 非交互输出被切到 UTF-8，内容完整（不是 gbk 的 ?）
    assert SYMBOLS in completed.stdout.decode("utf-8")


def test_configure_stdio_is_idempotent_and_never_raises(capsys):
    _configure_stdio()
    _configure_stdio()

    print(SYMBOLS, file=sys.stdout)
    assert SYMBOLS in capsys.readouterr().out


@pytest.mark.parametrize("stream_name", ["stdout", "stderr"])
def test_configure_stdio_handles_streams_without_reconfigure(stream_name, monkeypatch):
    """被替换成不支持 ``reconfigure`` 的对象（部分测试/嵌入场景）时不能抛错。"""

    class Plain:
        def write(self, _text: str) -> None: ...

        def isatty(self) -> bool:
            return False

    monkeypatch.setattr(sys, stream_name, Plain())

    _configure_stdio()  # 不应抛异常
