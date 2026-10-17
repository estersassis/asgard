"""Pilot promote gates: threshold check against candidate metrics."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from asgard.core.pilot.statistics import PilotMetrics

_DEFAULT_F1 = 0.90
_DEFAULT_KAPPA = 0.80


class GateThresholds(BaseModel):
    """Minimum scores the candidate full-loop metrics must meet.

    Unset optional gates (``None``) are ignored. F1 and κ default to the
    Forge mock values; pass ``None`` explicitly to disable either.
    """

    model_config = ConfigDict(extra="forbid")

    f1_min: float | None = _DEFAULT_F1
    kappa_min: float | None = _DEFAULT_KAPPA
    accuracy_min: float | None = None
    precision_min: float | None = None
    recall_min: float | None = None


class GateResult(BaseModel):
    """Outcome of comparing candidate metrics to configured thresholds."""

    model_config = ConfigDict(extra="forbid")

    passed: bool
    can_promote: bool
    failures: list[str] = Field(default_factory=list)
    thresholds: GateThresholds


def evaluate_gates(
    metrics: PilotMetrics,
    thresholds: GateThresholds | None = None,
) -> GateResult:
    """Return whether ``metrics`` clear every configured threshold."""
    gates = thresholds if thresholds is not None else GateThresholds()
    failures: list[str] = []
    checks: list[tuple[str, float | None, float]] = [
        ("f1", gates.f1_min, metrics.f1),
        ("kappa", gates.kappa_min, metrics.kappa),
        ("accuracy", gates.accuracy_min, metrics.accuracy),
        ("precision", gates.precision_min, metrics.precision),
        ("recall", gates.recall_min, metrics.recall),
    ]
    for name, minimum, value in checks:
        if minimum is None:
            continue
        if value < minimum:
            failures.append(name)
    passed = not failures
    return GateResult(
        passed=passed,
        can_promote=passed,
        failures=failures,
        thresholds=gates,
    )
