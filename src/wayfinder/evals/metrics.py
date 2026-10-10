"""评测指标与 baseline 回归门禁。对应 design.md 16.5 / 16.6。

门禁按**指标恶化**判定（而非数值下降）：越高越好的指标，下降即恶化；
越低越好的指标，上升即恶化。稳定指标（ScriptedLLM + Fake Tools 下确定性）
进硬门禁，波动指标只告警。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Sequence

if TYPE_CHECKING:  # 避免与 runner 形成运行时循环导入
    from wayfinder.evals.runner import CaseCheck


@dataclass(frozen=True)
class MetricSpec:
    name: str
    higher_is_better: bool
    stable: bool


METRIC_SPECS: tuple[MetricSpec, ...] = (
    # 工具选择拆成四个互补维度：序列 / 集合相等 / 召回 / 关键先后
    MetricSpec("Tool Sequence Accuracy", higher_is_better=True, stable=True),
    MetricSpec("Tool Selection Accuracy", higher_is_better=True, stable=True),
    MetricSpec("Tool Set Accuracy", higher_is_better=True, stable=True),
    MetricSpec("Precedence Accuracy", higher_is_better=True, stable=True),
    MetricSpec("Plan Validity Rate", higher_is_better=True, stable=True),
    MetricSpec("Argument Accuracy", higher_is_better=True, stable=True),
    MetricSpec("Memory Decision Accuracy", higher_is_better=True, stable=True),
    MetricSpec("Structured Output Validity Rate", higher_is_better=True, stable=True),
    MetricSpec("Extra Tool Call Rate", higher_is_better=False, stable=True),
    MetricSpec("Average Steps", higher_is_better=False, stable=True),
    # 含最终答案的指标：真 LLM 段波动明显，只告警
    MetricSpec("Plan Success Rate", higher_is_better=True, stable=False),
    MetricSpec("Assertion Pass Rate", higher_is_better=True, stable=False),
    MetricSpec("Average Latency", higher_is_better=False, stable=False),
)
SPECS_BY_NAME: dict[str, MetricSpec] = {spec.name: spec for spec in METRIC_SPECS}

STABLE_TOLERANCE = 1.0
FLAKY_TOLERANCE = 3.0


def _ratio(hits: int, total: int) -> float:
    return round(100.0 * hits / total, 2)


def compute_metrics(checks: Sequence["CaseCheck"]) -> dict[str, float]:
    """只计算「本批用例中确实有对应约束」的指标，不制造空指标。"""

    metrics: dict[str, float] = {}
    if not checks:
        return metrics

    def subset(name: str) -> list["CaseCheck"]:
        return [check for check in checks if check.checks.get(name) is not None]

    for check_name, metric_name in (
        ("tool_sequence", "Tool Sequence Accuracy"),
        ("tool_set", "Tool Selection Accuracy"),
        ("tool_recall", "Tool Set Accuracy"),
        ("precedence", "Precedence Accuracy"),
        ("plan_validity", "Plan Validity Rate"),
        ("plan_success", "Plan Success Rate"),
        ("arguments", "Argument Accuracy"),
        ("memory_delta", "Memory Decision Accuracy"),
    ):
        applicable = subset(check_name)
        if applicable:
            hits = sum(bool(check.checks[check_name]) for check in applicable)
            metrics[metric_name] = _ratio(hits, len(applicable))

    metrics["Structured Output Validity Rate"] = _ratio(
        sum(bool(check.outcome.structured_output_ok) for check in checks), len(checks)
    )

    total_calls = sum(len(check.outcome.tool_calls) for check in checks)
    if total_calls:
        extra_calls = sum(check.extra_calls for check in checks)
        metrics["Extra Tool Call Rate"] = round(100.0 * extra_calls / total_calls, 2)

    metrics["Average Steps"] = round(sum(check.outcome.steps for check in checks) / len(checks), 2)
    metrics["Average Latency"] = round(
        sum(check.outcome.latency_ms for check in checks) / len(checks), 1
    )

    asserted = [check for check in checks if check.assertion_results]
    if asserted:
        passed = sum(all(check.assertion_results) for check in asserted)
        metrics["Assertion Pass Rate"] = _ratio(passed, len(asserted))

    return metrics


@dataclass(frozen=True)
class MetricDelta:
    name: str
    baseline: float | None
    current: float | None
    higher_is_better: bool
    stable: bool
    tolerance: float

    @property
    def delta(self) -> float | None:
        """原始差值 ``current - baseline``。"""

        if self.baseline is None or self.current is None:
            return None
        return round(self.current - self.baseline, 2)

    @property
    def degradation(self) -> float:
        """方向化的恶化幅度（>0 表示变差）。"""

        if self.baseline is None or self.current is None:
            return 0.0
        if self.higher_is_better:
            return round(self.baseline - self.current, 2)
        return round(self.current - self.baseline, 2)

    @property
    def worsened(self) -> bool:
        return self.degradation > 0

    @property
    def within_tolerance(self) -> bool:
        """恶化幅度在容差内（稳定 1pt / 波动 3pt）。

        告警必须看容差：否则 0.01pt 的抖动也会报警，连续跑几次就会产生
        「告警疲劳」，真恶化反而被忽略。
        """

        return abs(self.degradation) <= self.tolerance

    @property
    def gate_failed(self) -> bool:
        """稳定指标恶化超过容差即硬门禁失败。"""

        return self.stable and self.degradation > self.tolerance


def compare_metrics(
    baseline: dict[str, float], current: dict[str, float]
) -> list[MetricDelta]:
    deltas: list[MetricDelta] = []
    for name in sorted(set(baseline) | set(current)):
        spec = SPECS_BY_NAME.get(name)
        higher_is_better = spec.higher_is_better if spec else True
        stable = spec.stable if spec else False
        tolerance = STABLE_TOLERANCE if stable else FLAKY_TOLERANCE
        deltas.append(
            MetricDelta(
                name=name,
                baseline=baseline.get(name),
                current=current.get(name),
                higher_is_better=higher_is_better,
                stable=stable,
                tolerance=tolerance,
            )
        )
    return deltas


def gate_failures(deltas: Sequence[MetricDelta]) -> list[MetricDelta]:
    return [delta for delta in deltas if delta.gate_failed]


def format_diff(deltas: Sequence[MetricDelta]) -> str:
    lines = ["| 指标 | baseline | current | Δ | 方向 | 判定 |", "|---|---:|---:|---:|---|---|"]
    for delta in deltas:
        if delta.gate_failed:
            verdict = "✗ 门禁失败"
        elif delta.worsened:
            verdict = "⚠ 恶化"
        else:
            verdict = "✓"
        base = "—" if delta.baseline is None else f"{delta.baseline:.2f}"
        current = "—" if delta.current is None else f"{delta.current:.2f}"
        change = "—" if delta.delta is None else f"{delta.delta:+.2f}"
        direction = "越高越好" if delta.higher_is_better else "越低越好"
        lines.append(f"| {delta.name} | {base} | {current} | {change} | {direction} | {verdict} |")
    return "\n".join(lines)


def save_metrics(path: str | Path, metrics: dict[str, float]) -> None:
    Path(path).write_text(
        json.dumps(metrics, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )


def load_metrics(path: str | Path) -> dict[str, float]:
    return json.loads(Path(path).read_text(encoding="utf-8"))
