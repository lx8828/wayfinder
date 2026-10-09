"""工具层。

v2 使用**显式注册**（``registry.register(spec)``），禁止 v1 那种
``tools/__init__.py`` import 副作用自动注册（design.md 第 6 节）。
"""

from first_agent.tools.registry import ToolRegistry

__all__ = ["ToolRegistry"]
