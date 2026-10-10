"""导入顺序回归护栏。

``core.engine`` 是组合根（依赖 runtime / memory），因此**不能**被 ``core/__init__``
急切导入，否则会形成 ``runtime.react → core.client → core.__init__ → core.engine →
runtime.react`` 的导入环。这里用独立解释器逐个顺序验证。
"""

from __future__ import annotations

import subprocess
import sys

import pytest

CASES = [
    "import wayfinder",
    "import wayfinder.runtime",
    "import wayfinder.core",
    "import wayfinder.memory",
    "import wayfinder.tools",
    "import wayfinder.providers",
    "import wayfinder.schemas",
    "import wayfinder.evals",
    "import wayfinder.main",
    "import wayfinder.runtime; from wayfinder.core.engine import ToolAgentRuntime",
    "import wayfinder.memory; from wayfinder.core.engine import ToolAgentRuntime",
    "from wayfinder.core.engine import ToolAgentRuntime; import wayfinder.runtime",
    "from wayfinder.core.engine import ToolAgentRuntime; import wayfinder.memory",
]


@pytest.mark.parametrize("code", CASES)
def test_import_order_is_safe(code):
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, f"{code}\n{result.stderr}"


def test_engine_is_reachable_from_documented_path():
    from wayfinder.core.engine import ToolAgentRuntime

    assert ToolAgentRuntime is not None
