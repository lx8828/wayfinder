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
    "import first_agent",
    "import first_agent.runtime",
    "import first_agent.core",
    "import first_agent.memory",
    "import first_agent.tools",
    "import first_agent.providers",
    "import first_agent.schemas",
    "import first_agent.evals",
    "import first_agent.main",
    "import first_agent.runtime; from first_agent.core.engine import ToolAgentRuntime",
    "import first_agent.memory; from first_agent.core.engine import ToolAgentRuntime",
    "from first_agent.core.engine import ToolAgentRuntime; import first_agent.runtime",
    "from first_agent.core.engine import ToolAgentRuntime; import first_agent.memory",
]


@pytest.mark.parametrize("code", CASES)
def test_import_order_is_safe(code):
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
    )

    assert result.returncode == 0, f"{code}\n{result.stderr}"


def test_engine_is_reachable_from_documented_path():
    from first_agent.core.engine import ToolAgentRuntime

    assert ToolAgentRuntime is not None
