"""评测编排、元数据与 baseline 回归门禁（design.md 16.5）。

产出目录（默认 ``src/first_agent/evals/``）：

```text
baseline.json / baseline_live.json    # 基线（进 git）
current.json  / current_live.json     # 本次结果（进 git，便于 diff）
diff.md       / diff_live.md          # 人类可读的对比表
```

门禁按**指标恶化**判定：稳定指标恶化 > 1pt 硬失败；波动指标恶化 > 3pt 只告警。
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

from first_agent.evals.case import EvalCase, load_cases
from first_agent.evals.metrics import (
    MetricDelta,
    compare_metrics,
    compute_metrics,
    format_diff,
    gate_failures,
    load_metrics,
    save_metrics,
)
from first_agent.evals.runner import CaseCheck, EngineFactory, run_cases_isolated

_DEFAULT_DIR = Path(__file__).resolve().parent


@dataclass
class SuiteRun:
    checks: list[CaseCheck]
    metrics: dict[str, float]
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def failures(self) -> list[CaseCheck]:
        return [check for check in self.checks if not check.passed]


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def git_commit() -> str:
    """当前提交；不在仓库中时返回 ``unknown``（不抛错）。"""

    try:
        completed = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - 环境相关
        return "unknown"
    return completed.stdout.strip() if completed.returncode == 0 else "unknown"


def prompt_version() -> str:
    """prompt 版本：把各处 system prompt 拼起来取摘要（design.md 16.5 要求存档）。"""

    from first_agent.core.context import DEFAULT_SYSTEM_PROMPT
    from first_agent.memory.adjudicator import SYSTEM_PROMPT as ADJUDICATOR_PROMPT
    from first_agent.memory.extractor import SYSTEM_PROMPT as EXTRACTOR_PROMPT
    from first_agent.runtime.plan_execute import PLANNER_TEMPLATE
    from first_agent.runtime import slots

    parts = [
        DEFAULT_SYSTEM_PROMPT,
        PLANNER_TEMPLATE,
        EXTRACTOR_PROMPT,
        ADJUDICATOR_PROMPT,
        getattr(slots, "SYSTEM_PROMPT", ""),
    ]
    return _digest("\n".join(parts))


def tool_schema_version(registry: Any) -> str:
    """工具契约版本：函数调用 schema 的摘要（schema 变了评测就要重跑）。"""

    return _digest(json.dumps(registry.schemas(), ensure_ascii=False, sort_keys=True))


def case_set_hash(path: str | Path) -> str:
    return _digest(Path(path).read_text(encoding="utf-8"))


def build_metadata(
    *,
    cases_path: str | Path,
    mode: str,
    model: str,
    registry: Any = None,
    case_count: int = 0,
) -> dict[str, Any]:
    return {
        "mode": mode,
        "model": model,
        "case_count": case_count,
        "case_set_hash": case_set_hash(cases_path),
        "prompt_version": prompt_version(),
        "tool_schema_version": tool_schema_version(registry) if registry is not None else "n/a",
        "git_commit": git_commit(),
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def run_suite(
    cases: Iterable[EvalCase], factory: EngineFactory, *, metadata: dict[str, Any]
) -> SuiteRun:
    case_list = list(cases)
    checks = run_cases_isolated(case_list, factory)
    metadata = {**metadata, "case_count": len(case_list)}
    return SuiteRun(checks=checks, metrics=compute_metrics(checks), metadata=metadata)


# -- 报告 ------------------------------------------------------------------


def failure_report(checks: Sequence[CaseCheck], *, limit: int = 12) -> str:
    """列出失败用例及**具体原因**（哪一项判定没过）。"""

    failures = [check for check in checks if not check.passed]
    if not failures:
        return "全部用例通过。"

    lines: list[str] = []
    for check in failures[:limit]:
        reasons: list[str] = []
        if check.error:
            reasons.append(f"error={check.error}")
        for name, value in check.checks.items():
            if value is False:
                reasons.append(f"{name}=✗")
        if check.assertion_results and not all(check.assertion_results):
            reasons.append("assertions=✗")
        if check.outcome.tool_errors:
            codes = "、".join(
                f"{item.get('tool')}:{item.get('error_code')}" for item in check.outcome.tool_errors
            )
            reasons.append(f"tool_errors=[{codes}]")
        if check.case.expected_status and check.outcome.status != check.case.expected_status:
            reasons.append(f"status={check.outcome.status}(期望 {check.case.expected_status})")
        got = ",".join(check.outcome.tool_calls) or "（无工具调用）"
        lines.append(f"- {check.case.case_id}: {'; '.join(reasons)} | 实际调用: {got}")

    if len(failures) > limit:
        lines.append(f"- …另有 {len(failures) - limit} 条失败用例未列出")
    return "\n".join(lines)


def metrics_report(metrics: dict[str, float]) -> str:
    lines = ["| 指标 | 值 |", "|---|---:|"]
    for name, value in sorted(metrics.items()):
        lines.append(f"| {name} | {value:.2f} |")
    return "\n".join(lines)


@dataclass
class GateResult:
    passed: bool
    deltas: list[MetricDelta] = field(default_factory=list)
    diff_markdown: str = ""

    @property
    def failed_deltas(self) -> list[MetricDelta]:
        return gate_failures(self.deltas)

    @property
    def warnings(self) -> list[MetricDelta]:
        """波动指标恶化**超过容差**才告警（design.md 16.5）。"""

        return [
            delta
            for delta in self.deltas
            if not delta.gate_failed and delta.worsened and not delta.within_tolerance
        ]


def evaluate_gate(
    baseline: dict[str, float] | None, current: dict[str, float]
) -> GateResult:
    """对照基线判定门禁；没有基线时视为通过（首次建立基线）。"""

    if not baseline:
        return GateResult(passed=True, deltas=[], diff_markdown="（无基线，已跳过对比）")
    deltas = compare_metrics(baseline, current)
    failures = gate_failures(deltas)
    return GateResult(passed=not failures, deltas=deltas, diff_markdown=format_diff(deltas))


# -- 落盘 ------------------------------------------------------------------


def write_outputs(
    run: SuiteRun,
    *,
    out_dir: str | Path = _DEFAULT_DIR,
    suffix: str = "",
    gate: GateResult | None = None,
) -> dict[str, Path]:
    """写 ``current*.json`` / ``diff*.md``（基线由 ``--update-baseline`` 决定是否覆盖）。"""

    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    current_path = directory / f"current{suffix}.json"

    payload = {"metadata": run.metadata, "metrics": run.metrics}
    current_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )

    paths = {"current": current_path}
    if gate is not None:
        diff_path = directory / f"diff{suffix}.md"
        body = [
            f"# 评测对比（{run.metadata.get('mode', 'unknown')}）",
            "",
            f"- 生成时间：{run.metadata.get('generated_at', '—')}",
            f"- git commit：{run.metadata.get('git_commit', '—')}",
            f"- model：{run.metadata.get('model', '—')}",
            f"- case set hash：{run.metadata.get('case_set_hash', '—')}",
            f"- prompt version：{run.metadata.get('prompt_version', '—')}",
            f"- tool schema version：{run.metadata.get('tool_schema_version', '—')}",
            "",
            "## 指标对比",
            "",
            gate.diff_markdown,
            "",
            "## 门禁",
            "",
            "通过 ✅" if gate.passed else f"失败 ❌（{len(gate.failed_deltas)} 项稳定指标恶化）",
            "",
            "## 失败用例",
            "",
            failure_report(run.checks),
            "",
        ]
        diff_path.write_text("\n".join(body), encoding="utf-8")
        paths["diff"] = diff_path
    return paths


def load_baseline(path: str | Path) -> dict[str, float] | None:
    """读取基线；文件不存在返回 ``None``。"""

    file = Path(path)
    if not file.exists():
        return None
    payload = json.loads(file.read_text(encoding="utf-8"))
    if isinstance(payload, dict) and "metrics" in payload:
        return dict(payload["metrics"])
    return dict(payload)


def save_baseline(path: str | Path, run: SuiteRun) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {"metadata": run.metadata, "metrics": run.metrics},
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def load_cases_file(path: str | Path) -> list[EvalCase]:
    return load_cases(path)


__all__ = [
    "SuiteRun",
    "GateResult",
    "build_metadata",
    "case_set_hash",
    "evaluate_gate",
    "failure_report",
    "git_commit",
    "load_baseline",
    "load_cases_file",
    "metrics_report",
    "prompt_version",
    "run_suite",
    "save_baseline",
    "save_metrics",
    "tool_schema_version",
    "write_outputs",
]
