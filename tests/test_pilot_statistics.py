"""Fixed-vector checks for binary pilot metrics."""

import pytest

from asgard.core.pilot.statistics import (
    Confusion,
    PilotStatistics,
    compute_metrics,
    confusion_matrix,
)


def test_confusion_and_metrics_known_vector() -> None:
    y_true = [True, True, True, False, False, False, True, False]
    y_pred = [True, True, False, False, False, True, True, False]

    confusion = confusion_matrix(y_true, y_pred)
    assert confusion == Confusion(tp=3, fp=1, fn=1, tn=3)

    metrics = compute_metrics(y_true, y_pred)
    assert metrics.confusion == confusion
    assert metrics.accuracy == pytest.approx(0.75)
    assert metrics.precision == pytest.approx(0.75)
    assert metrics.recall == pytest.approx(0.75)
    assert metrics.f1 == pytest.approx(0.75)
    assert metrics.kappa == pytest.approx(0.5)
    assert metrics == compute_metrics(y_true, y_pred)
    assert isinstance(metrics, PilotStatistics)


def test_perfect_agreement() -> None:
    y = [True, True, False, False, True]
    metrics = compute_metrics(y, list(y))
    assert metrics.confusion == Confusion(tp=3, fp=0, fn=0, tn=2)
    assert metrics.accuracy == 1.0
    assert metrics.precision == 1.0
    assert metrics.recall == 1.0
    assert metrics.f1 == 1.0
    assert metrics.kappa == 1.0


def test_all_positive_agreement_kappa_is_one() -> None:
    y = [True, True, True]
    metrics = compute_metrics(y, list(y))
    assert metrics.kappa == 1.0
    assert metrics.f1 == 1.0


def test_zero_division_defaults_to_zero() -> None:
    # No predicted positives → precision 0; no true positives → recall 0.
    y_true = [False, False, False]
    y_pred = [False, False, False]
    metrics = compute_metrics(y_true, y_pred)
    assert metrics.precision == 0.0
    assert metrics.recall == 0.0
    assert metrics.f1 == 0.0
    assert metrics.accuracy == 1.0
    assert metrics.kappa == 1.0

    y_true = [True, True, True]
    y_pred = [False, False, False]
    metrics = compute_metrics(y_true, y_pred)
    assert metrics.precision == 0.0
    assert metrics.recall == 0.0
    assert metrics.f1 == 0.0
    assert metrics.accuracy == 0.0
    assert metrics.kappa == 0.0


def test_length_mismatch_and_empty() -> None:
    with pytest.raises(ValueError, match="length mismatch"):
        compute_metrics([True], [True, False])
    with pytest.raises(ValueError, match="non-empty"):
        compute_metrics([], [])
