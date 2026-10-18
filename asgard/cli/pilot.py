"""Run pilot validation and promote a candidate prompt into config."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from asgard.core.config import load_dotenv, load_run
from asgard.core.pilot.gate import GateThresholds, PromoteError, promote
from asgard.core.pilot.runner import PilotResult, run_pilot
from asgard.core.pilot.set import BUNDLED_SETS
from asgard.providers.factory import LLMFactory

_DEFAULT_PILOT_SET = "binary_v2"
_DEFAULT_PILOT_ROOT = Path("runs") / "_pilot"


def run_pilot_command(
    *,
    candidate: str,
    active: str | None = None,
    pilot_set: str | Path = _DEFAULT_PILOT_SET,
    config: Path | None = None,
    f1_min: float | None = 0.90,
    kappa_min: float | None = 0.80,
    accuracy_min: float | None = None,
    precision_min: float | None = None,
    recall_min: float | None = None,
    output_dir: Path | None = None,
    registry_dir: Path | None = None,
    handlers: dict[str, Any] | None = None,
    dotenv: Path | None = Path(".env"),
) -> PilotResult:
    """Load LLM settings from config, run the pilot, and print a summary."""
    if dotenv is not None:
        load_dotenv(dotenv)
    run_config, credentials = load_run(config)
    active_id = (active or run_config.prompts.classifier).strip() or "default"
    root = registry_dir
    if root is None:
        root = run_config.prompts.registry_dir

    thresholds = GateThresholds(
        f1_min=f1_min,
        kappa_min=kappa_min,
        accuracy_min=accuracy_min,
        precision_min=precision_min,
        recall_min=recall_min,
    )
    factory = LLMFactory(run_config.llm, credentials, handlers=handlers or {})
    result = run_pilot(
        pilot_set,
        candidate=candidate,
        active=active_id,
        factory=factory,
        registry_dir=root,
        max_iterations=run_config.max_iterations,
        thresholds=thresholds,
        output_dir=output_dir,
    )
    _print_run(result)
    return result


def promote_pilot_command(
    *,
    candidate: str,
    config: Path,
    from_result: Path | None = None,
    force: bool = False,
    role: str = "classifier",
) -> None:
    """Promote ``candidate`` into ``config`` when a pilot run cleared the gates."""
    evidence = from_result
    if evidence is None and not force:
        evidence = _latest_pilot_result()
        if evidence is None:
            raise PromoteError(
                "no pilot result found under runs/_pilot/; "
                "pass --from-result PATH or --force"
            )

    if evidence is not None and not force:
        _require_matching_candidate(evidence, candidate)

    result = promote(
        config,
        version_id=candidate,
        role=role,
        from_result=evidence,
        force=force,
    )
    status = "forced" if force and not result.passed else "ok"
    print(f"promoted {role}={candidate}\t{status}\tconfig={config}")


def _print_run(result: PilotResult) -> None:
    active = result.active
    candidate = result.candidate
    single = result.candidate_single_pass
    confusion = result.confusion_candidate
    gates = result.gates

    print(f"pilot_set\t{result.pilot_set_id}")
    print(f"run_id\t{result.run_id}")
    print(f"output_dir\t{result.output_dir}")
    print(f"active_prompt\t{result.active_prompt}")
    print(f"candidate_prompt\t{result.candidate_prompt}")
    print(
        "active\t"
        f"f1={_fmt(active.f1)}\tkappa={_fmt(active.kappa)}\t"
        f"acc={_fmt(active.accuracy)}"
    )
    print(
        "candidate\t"
        f"f1={_fmt(candidate.f1)}\tkappa={_fmt(candidate.kappa)}\t"
        f"acc={_fmt(candidate.accuracy)}"
    )
    print(
        "delta\t"
        f"f1={_fmt(candidate.f1 - active.f1)}\t"
        f"kappa={_fmt(candidate.kappa - active.kappa)}"
    )
    print(
        "confusion_candidate\t"
        f"tp={confusion.tp}\tfp={confusion.fp}\t"
        f"fn={confusion.fn}\ttn={confusion.tn}"
    )
    print(
        "single_pass\t"
        f"f1={_fmt(single.f1)}\t"
        f"full_loop_f1={_fmt(candidate.f1)}"
    )
    gate_status = "PASS" if gates.passed else "FAIL"
    failed = ",".join(gates.failures) if gates.failures else "-"
    print(f"gates\t{gate_status}\tfailures={failed}\tcan_promote={gates.can_promote}")


def _fmt(value: float) -> str:
    return f"{value:.4f}"


def _latest_pilot_result(root: Path = _DEFAULT_PILOT_ROOT) -> Path | None:
    if not root.is_dir():
        return None
    metrics = [path for path in root.glob("*/metrics.json") if path.is_file()]
    if not metrics:
        return None
    return max(metrics, key=lambda path: path.stat().st_mtime)


def _require_matching_candidate(metrics_path: Path, candidate: str) -> None:
    try:
        data = metrics_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromoteError(f"cannot read pilot result: {metrics_path}") from exc
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise PromoteError(f"invalid pilot result JSON: {exc}") from None
    if not isinstance(payload, dict):
        return
    recorded = payload.get("candidate_prompt")
    if isinstance(recorded, str) and recorded.strip() and recorded.strip() != candidate.strip():
        raise PromoteError(
            f"pilot result candidate {recorded!r} does not match --candidate {candidate!r}"
        )


def bundled_pilot_set_help() -> str:
    known = ", ".join(sorted(BUNDLED_SETS)) or _DEFAULT_PILOT_SET
    return f"bundled id ({known}) or path to a pilot-set JSON"
