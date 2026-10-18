"""Run active vs candidate classifier prompts against a pilot set."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from asgard.core.classification import classify
from asgard.core.events import ClassifyProgress, Event
from asgard.core.pilot.gate import GateResult, GateThresholds, evaluate_gates
from asgard.core.pilot.set import PilotSet, load_pilot_set
from asgard.core.pilot.statistics import Confusion, PilotMetrics, compute_metrics
from asgard.core.prompt_registry import PromptRegistry, default_registry_root
from asgard.core.stage import RunContext
from asgard.core.stages.classify import ClassifyStage
from asgard.providers.errors import ProviderError
from asgard.providers.factory import LLMFactory
from asgard.providers.models import LLMSettings, RoleSpec

_CAUGHT = (ProviderError, ValueError, ValidationError, TypeError)

_DEFAULT_MAX_ITERATIONS = 3

PilotMode = Literal["classifier", "full"]
PilotPhase = Literal["active", "candidate", "candidate_single_pass", "writing"]

_PHASE_LABELS: dict[PilotPhase, str] = {
    "active": "Active prompt",
    "candidate": "Candidate prompt",
    "candidate_single_pass": "Candidate single-pass",
    "writing": "Writing metrics",
}


class PilotProgress(BaseModel):
    """Live progress for a pilot run (per phase and overall)."""

    model_config = ConfigDict(extra="forbid")

    phase: PilotPhase
    phase_label: str
    done: int
    total: int
    overall_done: int
    overall_total: int
    message: str


class PilotPrediction(BaseModel):
    """Per-PR human label and model decisions."""

    model_config = ConfigDict(extra="forbid")

    pr_id: str
    label: bool
    active: bool | None
    candidate: bool | None
    candidate_single_pass: bool | None


class PilotResult(BaseModel):
    """Full pilot comparison for one candidate against the active prompt."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    pilot_set_id: str
    mode: PilotMode = "full"
    active_prompt: str
    candidate_prompt: str
    active: PilotMetrics
    candidate: PilotMetrics
    candidate_single_pass: PilotMetrics
    confusion_candidate: Confusion
    gates: GateResult
    predictions: list[PilotPrediction] = Field(default_factory=list)
    output_dir: Path


def run_pilot(
    pilot_set: PilotSet | str | Path,
    *,
    candidate: str,
    factory: LLMFactory,
    active: str | None = None,
    registry_dir: Path | None = None,
    max_iterations: int = _DEFAULT_MAX_ITERATIONS,
    thresholds: GateThresholds | None = None,
    output_dir: Path | None = None,
    emit: Callable[[Event], None] | None = None,
    mode: PilotMode = "full",
    on_progress: Callable[[PilotProgress], None] | None = None,
) -> PilotResult:
    """Compare active vs candidate prompts on a pilot set (temperature forced to 0).

    ``mode="full"`` runs the evaluator-optimizer loop for both prompts, then a
    single-pass ``classify()`` (no evaluator) for the candidate.

    ``mode="classifier"`` runs single-pass ``classify()`` only for both prompts
    (no evaluator). Writes ``metrics.json`` and ``predictions.jsonl`` under
    ``output_dir`` (default ``runs/_pilot/<stamp>/``).
    """
    if max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")
    if mode not in ("classifier", "full"):
        raise ValueError("mode must be 'classifier' or 'full'")

    loaded = pilot_set if isinstance(pilot_set, PilotSet) else load_pilot_set(pilot_set)
    active_id = (active or "default").strip() or "default"
    candidate_id = candidate.strip()
    if not candidate_id:
        raise ValueError("candidate prompt id must not be empty")

    root = Path(registry_dir) if registry_dir is not None else default_registry_root()
    registry = PromptRegistry(root)
    # Fail early if either version is missing.
    registry.get("classifier", active_id)
    registry.get("classifier", candidate_id)
    active_body = registry.resolve("classifier", active_id)
    candidate_body = registry.resolve("classifier", candidate_id)

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = Path(output_dir) if output_dir is not None else Path("runs") / "_pilot" / run_id
    out.mkdir(parents=True, exist_ok=True)

    zero_factory = _zero_temperature_factory(factory)
    sink = emit or (lambda _event: None)
    n = len(loaded.items)
    report = _progress_reporter(on_progress, item_count=n, mode=mode)

    if mode == "classifier":
        active_preds = _run_single_pass(
            loaded,
            system_prompt=active_body,
            factory=zero_factory,
            emit=sink,
            on_item=lambda done, total: report("active", done, total),
        )
        candidate_preds = _run_single_pass(
            loaded,
            system_prompt=candidate_body,
            factory=zero_factory,
            emit=sink,
            on_item=lambda done, total: report("candidate", done, total),
        )
        single_preds = dict(candidate_preds)
    else:
        active_preds = _run_full_loop(
            loaded,
            prompt_id=active_id,
            factory=zero_factory,
            registry_dir=root,
            max_iterations=max_iterations,
            run_dir=out / "active",
            emit=sink,
            on_item=lambda done, total: report("active", done, total),
        )
        candidate_preds = _run_full_loop(
            loaded,
            prompt_id=candidate_id,
            factory=zero_factory,
            registry_dir=root,
            max_iterations=max_iterations,
            run_dir=out / "candidate",
            emit=sink,
            on_item=lambda done, total: report("candidate", done, total),
        )
        single_preds = _run_single_pass(
            loaded,
            system_prompt=candidate_body,
            factory=zero_factory,
            emit=sink,
            on_item=lambda done, total: report("candidate_single_pass", done, total),
        )

    report("writing", 1, 1)

    y_true = [item.label for item in loaded.items]
    pr_ids = [item.pr.context.pr_id for item in loaded.items]
    active_metrics = compute_metrics(y_true, [_require(active_preds, pr_id) for pr_id in pr_ids])
    candidate_metrics = compute_metrics(
        y_true, [_require(candidate_preds, pr_id) for pr_id in pr_ids]
    )
    single_metrics = compute_metrics(
        y_true, [_require(single_preds, pr_id) for pr_id in pr_ids]
    )
    gates = evaluate_gates(candidate_metrics, thresholds)

    predictions = [
        PilotPrediction(
            pr_id=pr_id,
            label=label,
            active=active_preds.get(pr_id),
            candidate=candidate_preds.get(pr_id),
            candidate_single_pass=single_preds.get(pr_id),
        )
        for pr_id, label in zip(pr_ids, y_true, strict=True)
    ]

    result = PilotResult(
        run_id=run_id if output_dir is None else out.name,
        pilot_set_id=loaded.id,
        mode=mode,
        active_prompt=active_id,
        candidate_prompt=candidate_id,
        active=active_metrics,
        candidate=candidate_metrics,
        candidate_single_pass=single_metrics,
        confusion_candidate=candidate_metrics.confusion,
        gates=gates,
        predictions=predictions,
        output_dir=out,
    )
    _write_artifacts(out, result)
    return result


def _progress_reporter(
    on_progress: Callable[[PilotProgress], None] | None,
    *,
    item_count: int,
    mode: PilotMode,
) -> Callable[[PilotPhase, int, int], None]:
    # Overall counts only PR classifications (not the brief "writing" step).
    # classifier: active + candidate = 2×N; full: + single-pass = 3×N.
    if mode == "classifier":
        pr_phases: tuple[PilotPhase, ...] = ("active", "candidate")
    else:
        pr_phases = ("active", "candidate", "candidate_single_pass")
    overall_total = item_count * len(pr_phases)
    completed_before = {pr_phases[0]: 0}
    for index, phase in enumerate(pr_phases[1:], start=1):
        completed_before[phase] = item_count * index
    completed_before["writing"] = overall_total

    def report(phase: PilotPhase, done: int, total: int) -> None:
        if on_progress is None:
            return
        capped_total = max(total, 0)
        capped_done = max(0, min(done, capped_total if capped_total else done))
        if phase == "writing":
            overall_done = overall_total
        else:
            overall_done = completed_before.get(phase, 0) + capped_done
            if overall_done > overall_total:
                overall_done = overall_total
        label = _PHASE_LABELS.get(phase, phase)
        if phase == "writing":
            message = f"Running pilot · {label}"
        else:
            message = (
                f"Running pilot · {label} · {capped_done}/{capped_total} PRs"
                f" · overall {overall_done}/{overall_total}"
            )
        on_progress(
            PilotProgress(
                phase=phase,
                phase_label=label,
                done=capped_done,
                total=capped_total,
                overall_done=overall_done,
                overall_total=overall_total,
                message=message,
            )
        )

    report(pr_phases[0], 0, item_count)
    return report


def _run_full_loop(
    pilot_set: PilotSet,
    *,
    prompt_id: str,
    factory: LLMFactory,
    registry_dir: Path,
    max_iterations: int,
    run_dir: Path,
    emit: Callable[[Event], None],
    on_item: Callable[[int, int], None] | None = None,
) -> dict[str, bool]:
    """Drive :class:`ClassifyStage` / ``_review`` with the selected classifier prompt."""
    run_dir.mkdir(parents=True, exist_ok=True)
    _write_prompt_config(run_dir, classifier=prompt_id, registry_dir=registry_dir)
    total = len(pilot_set.items)
    if on_item is not None:
        on_item(0, total)

    def sink(event: Event) -> None:
        emit(event)
        if on_item is None or not isinstance(event, ClassifyProgress):
            return
        if event.kind != "pull":
            return
        # 1-based index of the PR currently being classified.
        on_item(min(event.index, event.total), event.total)

    stage = ClassifyStage(max_iterations, factory, batch_size=1)
    ctx = RunContext(run_dir, sink)
    pulls = [item.pr for item in pilot_set.items]
    payloads = stage.run(ctx, pulls)

    predictions: dict[str, bool] = {}
    for payload in payloads:
        pr_id = str(payload["pr_id"])
        predictions[pr_id] = _decision_from_payload(payload)
    if on_item is not None:
        on_item(total, total)
    return predictions


def _run_single_pass(
    pilot_set: PilotSet,
    *,
    system_prompt: str,
    factory: LLMFactory,
    emit: Callable[[Event], None],
    on_item: Callable[[int, int], None] | None = None,
) -> dict[str, bool]:
    """One ``classify()`` call per PR — no evaluator."""
    predictions: dict[str, bool] = {}
    total = len(pilot_set.items)
    if on_item is not None:
        on_item(0, total)
    for index, item in enumerate(pilot_set.items, start=1):
        if on_item is not None:
            on_item(index, total)
        pr_id = item.pr.context.pr_id
        try:
            result = classify(
                item.pr,
                factory,
                emit=emit,
                system_prompt=system_prompt,
            )
        except _CAUGHT:
            predictions[pr_id] = False
        else:
            predictions[pr_id] = result.has_security_discussion
    return predictions


def _write_prompt_config(run_dir: Path, *, classifier: str, registry_dir: Path) -> None:
    payload = {
        "prompts": {
            "classifier": classifier,
            "evaluator": "default",
            "synthesizer": "default",
            "registry_dir": str(registry_dir),
        }
    }
    path = run_dir / "config.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def _write_artifacts(output_dir: Path, result: PilotResult) -> None:
    metrics_path = output_dir / "metrics.json"
    metrics_path.write_text(
        json.dumps(result.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    lines = [
        json.dumps(prediction.model_dump(mode="json"), ensure_ascii=False)
        for prediction in result.predictions
    ]
    (output_dir / "predictions.jsonl").write_text(
        "".join(line + "\n" for line in lines),
        encoding="utf-8",
    )


def _zero_temperature_factory(factory: LLMFactory) -> LLMFactory:
    return LLMFactory(
        _zero_temperature_settings(factory.settings),
        factory.credentials,
        handlers=factory.handlers,
        sleep=factory.sleep,
    )


def _zero_temperature_settings(settings: LLMSettings) -> LLMSettings:
    return LLMSettings(
        default=_zero_spec(settings.default),
        classifier=_zero_spec(settings.classifier) if settings.classifier else None,
        evaluator=_zero_spec(settings.evaluator) if settings.evaluator else None,
        synthesizer=_zero_spec(settings.synthesizer) if settings.synthesizer else None,
    )


def _zero_spec(spec: RoleSpec) -> RoleSpec:
    if spec.temperature == 0:
        return spec
    return spec.model_copy(update={"temperature": 0.0})


def _decision_from_payload(payload: dict[str, Any]) -> bool:
    classification = payload.get("classification")
    if not isinstance(classification, dict):
        return False
    decision = classification.get("has_security_discussion")
    return decision is True


def _require(predictions: dict[str, bool], pr_id: str) -> bool:
    if pr_id not in predictions:
        raise KeyError(f"missing prediction for pr_id {pr_id!r}")
    return predictions[pr_id]
