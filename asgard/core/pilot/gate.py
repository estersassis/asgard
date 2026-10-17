"""Pilot promote gates: threshold check + promote-into-config."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from asgard.core.config import dump_toml
from asgard.core.pilot.statistics import PilotMetrics
from asgard.core.prompt_registry import ROLES

_DEFAULT_F1 = 0.90
_DEFAULT_KAPPA = 0.80


class PromoteError(Exception):
    """Candidate failed gates or the config could not be updated."""


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


def promote(
    config_path: Path | str,
    *,
    version_id: str,
    role: str = "classifier",
    gates: GateResult | None = None,
    metrics: PilotMetrics | None = None,
    thresholds: GateThresholds | None = None,
    from_result: Path | str | None = None,
    force: bool = False,
) -> GateResult:
    """Update ``[prompts].<role>`` in a TOML config when gates pass.

    Evidence that gates cleared must be supplied via ``gates``, ``metrics``
    (re-evaluated with ``thresholds``), or a pilot ``metrics.json`` /
    result path in ``from_result``. Without evidence, ``force=True`` is
    required. On failure the config file is left untouched.
    """
    if role not in ROLES:
        raise PromoteError(f"unknown prompt role: {role}")
    candidate = version_id.strip()
    if not candidate:
        raise PromoteError("version_id must not be empty")

    result = _resolve_gates(
        gates=gates,
        metrics=metrics,
        thresholds=thresholds,
        from_result=from_result,
        force=force,
    )
    if not result.can_promote and not force:
        failed = ", ".join(result.failures) if result.failures else "gates"
        raise PromoteError(f"cannot promote: {failed} below threshold")

    path = Path(config_path)
    if not path.is_file():
        raise PromoteError(f"config not found: {path}")
    if path.suffix != ".toml":
        raise PromoteError(f"promote requires a .toml config, got {path.suffix or path.name}")

    text = path.read_text(encoding="utf-8")
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PromoteError(f"invalid config TOML: {exc}") from None
    if not isinstance(data, dict):
        raise PromoteError("settings file must be a table")

    prompts = dict(data.get("prompts") or {})
    prompts[role] = candidate
    data["prompts"] = prompts
    data.pop("github_token", None)
    data.pop("keys", None)
    path.write_text(dump_toml(data), encoding="utf-8")
    return result


def _resolve_gates(
    *,
    gates: GateResult | None,
    metrics: PilotMetrics | None,
    thresholds: GateThresholds | None,
    from_result: Path | str | None,
    force: bool,
) -> GateResult:
    if gates is not None:
        return gates
    if from_result is not None:
        return _gates_from_result(Path(from_result), thresholds)
    if metrics is not None:
        return evaluate_gates(metrics, thresholds)
    if force:
        return GateResult(
            passed=True,
            can_promote=True,
            failures=[],
            thresholds=thresholds if thresholds is not None else GateThresholds(),
        )
    raise PromoteError(
        "promote requires gates, metrics, or from_result (pass force=True to skip)"
    )


def _gates_from_result(
    path: Path,
    thresholds: GateThresholds | None,
) -> GateResult:
    if not path.is_file():
        raise PromoteError(f"pilot result not found: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PromoteError(f"invalid pilot result JSON: {exc}") from None
    if not isinstance(data, dict):
        raise PromoteError("pilot result must be a JSON object")

    raw_gates = data.get("gates")
    if isinstance(raw_gates, dict):
        try:
            return GateResult.model_validate(raw_gates)
        except ValidationError as exc:
            raise PromoteError(f"invalid gates in pilot result: {exc}") from None

    raw_candidate = data.get("candidate")
    if isinstance(raw_candidate, dict):
        try:
            metrics = PilotMetrics.model_validate(raw_candidate)
        except ValidationError as exc:
            raise PromoteError(f"invalid candidate metrics in pilot result: {exc}") from None
        return evaluate_gates(metrics, thresholds)

    raise PromoteError("pilot result must include gates or candidate metrics")
