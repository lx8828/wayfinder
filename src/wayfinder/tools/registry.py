"""工具注册表。对应 design.md 6 / 11.1。

- 只做注册与查询，不负责执行（执行在后续阶段的 tool executor）
- 显式注册，注册顺序即 schema 顺序
"""

from __future__ import annotations

from wayfinder.schemas.tool import ToolSpec


class ToolRegistry:
    """``name -> ToolSpec`` 的显式注册表。"""

    def __init__(self) -> None:
        self._specs: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> ToolSpec:
        """注册工具；重名直接报错，避免静默覆盖。"""

        if not isinstance(spec, ToolSpec):
            raise TypeError(f"只能注册 ToolSpec，收到 {type(spec).__name__}")
        if spec.name in self._specs:
            raise ValueError(f"工具重复注册：{spec.name}")
        self._specs[spec.name] = spec
        return spec

    def get(self, name: str) -> ToolSpec | None:
        return self._specs.get(name)

    def names(self) -> list[str]:
        return list(self._specs)

    def schemas(self) -> list[dict]:
        """全部工具的 function calling 描述（design.md 11.1）。"""

        return [spec.to_function_schema() for spec in self._specs.values()]

    def __contains__(self, name: object) -> bool:
        return name in self._specs

    def __len__(self) -> int:
        return len(self._specs)
