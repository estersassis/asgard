"""Gate thresholds and promote-into-config refusal."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asgard.core.pilot.gate import (
    GateThresholds,
    PromoteError,
    evaluate_gates,
    promote,
)
from asgard.core.pilot.statistics import Confusion, PilotMetrics


def _metrics(*, f1: float, kappa: float, accuracy: float = 0.95) -> PilotMetrics:
    return PilotMetrics(
        accuracy=accuracy,
        precision=f1,
        recall=f1,
        f1=f1,
        kappa=kappa,
        confusion=Confusion(tp=10, fp=0, fn=0, tn=5),
    )


def _config(tmp_path: Path, *, classifier: str = "default") -> Path:
    path = tmp_path / "asgard.toml"
    path.write_text(
        "\n".join(
            [
                'repo = "owner/name"',
                f'output_dir = "{tmp_path / "out"}"',
                "",
                "[prompts]",
                f'classifier = "{classifier}"',
                'evaluator = "default"',
                "",
                "[llm.default]",
                'provider = "fake"',
                'model = "fake"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_below_f1_or_kappa_cannot_promote() -> None:
    low_f1 = evaluate_gates(_metrics(f1=0.89, kappa=0.95))
    assert low_f1.passed is False
    assert low_f1.can_promote is False
    assert low_f1.failures == ["f1"]

    low_kappa = evaluate_gates(_metrics(f1=0.95, kappa=0.79))
    assert low_kappa.can_promote is False
    assert low_kappa.failures == ["kappa"]

    both = evaluate_gates(_metrics(f1=0.5, kappa=0.5))
    assert both.can_promote is False
    assert both.failures == ["f1", "kappa"]


def test_defaults_and_optional_gates() -> None:
    passing = evaluate_gates(_metrics(f1=0.90, kappa=0.80))
    assert passing.can_promote is True
    assert passing.failures == []
    assert passing.thresholds.f1_min == 0.90
    assert passing.thresholds.kappa_min == 0.80
    assert passing.thresholds.accuracy_min is None

    # Unset optional gates are ignored even when accuracy is low.
    ignore_accuracy = evaluate_gates(
        _metrics(f1=0.95, kappa=0.90, accuracy=0.10),
        GateThresholds(),
    )
    assert ignore_accuracy.can_promote is True

    with_accuracy = evaluate_gates(
        _metrics(f1=0.95, kappa=0.90, accuracy=0.10),
        GateThresholds(accuracy_min=0.50),
    )
    assert with_accuracy.can_promote is False
    assert with_accuracy.failures == ["accuracy"]

    # Explicit None disables the default F1 / κ gates.
    disabled = evaluate_gates(
        _metrics(f1=0.0, kappa=0.0),
        GateThresholds(f1_min=None, kappa_min=None),
    )
    assert disabled.can_promote is True


def test_promote_refuses_and_does_not_rewrite_config(tmp_path: Path) -> None:
    config = _config(tmp_path, classifier="active-prompt")
    before = config.read_text(encoding="utf-8")
    gates = evaluate_gates(_metrics(f1=0.50, kappa=0.40))

    with pytest.raises(PromoteError, match="cannot promote"):
        promote(
            config,
            version_id="candidate-v1",
            gates=gates,
        )

    assert config.read_text(encoding="utf-8") == before
    assert 'classifier = "active-prompt"' in before


def test_promote_updates_prompts_when_gates_pass(tmp_path: Path) -> None:
    config = _config(tmp_path, classifier="active-prompt")
    gates = evaluate_gates(_metrics(f1=0.95, kappa=0.85))

    result = promote(config, version_id="candidate-v1", gates=gates)
    assert result.can_promote is True

    text = config.read_text(encoding="utf-8")
    assert 'classifier = "candidate-v1"' in text
    assert 'classifier = "active-prompt"' not in text
    assert "[prompts]" in text
    assert 'provider = "fake"' in text


def test_promote_from_metrics_and_from_result(tmp_path: Path) -> None:
    config = _config(tmp_path)
    metrics = _metrics(f1=0.95, kappa=0.85)
    promote(config, version_id="from-metrics", metrics=metrics)
    assert 'classifier = "from-metrics"' in config.read_text(encoding="utf-8")

    result_path = tmp_path / "metrics.json"
    result_path.write_text(
        json.dumps(
            {
                "gates": evaluate_gates(metrics).model_dump(mode="json"),
                "candidate": metrics.model_dump(mode="json"),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    promote(config, version_id="from-result", from_result=result_path)
    assert 'classifier = "from-result"' in config.read_text(encoding="utf-8")

    failing = tmp_path / "fail.json"
    failing.write_text(
        json.dumps(
            {
                "candidate": _metrics(f1=0.1, kappa=0.1).model_dump(mode="json"),
            }
        )
        + "\n",
        encoding="utf-8",
    )
    before = config.read_text(encoding="utf-8")
    with pytest.raises(PromoteError, match="cannot promote"):
        promote(config, version_id="should-not-land", from_result=failing)
    assert config.read_text(encoding="utf-8") == before


def test_promote_force_skips_gate_check(tmp_path: Path) -> None:
    config = _config(tmp_path, classifier="active")
    promote(config, version_id="forced", force=True)
    assert 'classifier = "forced"' in config.read_text(encoding="utf-8")
