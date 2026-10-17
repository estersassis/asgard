"""Binary classification metrics for pilot validation (no sklearn)."""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field


class Confusion(BaseModel):
    """2×2 confusion counts with security class = ``True``."""

    model_config = ConfigDict(extra="forbid")

    tp: int = Field(ge=0)
    fp: int = Field(ge=0)
    fn: int = Field(ge=0)
    tn: int = Field(ge=0)

    @property
    def n(self) -> int:
        return self.tp + self.fp + self.fn + self.tn


class PilotMetrics(BaseModel):
    """Accuracy / P / R / F1 / κ for the security (``True``) class."""

    model_config = ConfigDict(extra="forbid")

    accuracy: float
    precision: float
    recall: float
    f1: float
    kappa: float
    confusion: Confusion


def confusion_matrix(y_true: Sequence[bool], y_pred: Sequence[bool]) -> Confusion:
    """Build a 2×2 confusion matrix (positive class = ``True``)."""
    _require_aligned(y_true, y_pred)
    tp = fp = fn = tn = 0
    for truth, pred in zip(y_true, y_pred, strict=True):
        if truth and pred:
            tp += 1
        elif not truth and pred:
            fp += 1
        elif truth and not pred:
            fn += 1
        else:
            tn += 1
    return Confusion(tp=tp, fp=fp, fn=fn, tn=tn)


def compute_metrics(y_true: Sequence[bool], y_pred: Sequence[bool]) -> PilotMetrics:
    """Compute binary pilot metrics from aligned boolean label vectors."""
    confusion = confusion_matrix(y_true, y_pred)
    n = confusion.n
    if n == 0:
        raise ValueError("y_true and y_pred must be non-empty")

    accuracy = (confusion.tp + confusion.tn) / n
    precision = _safe_div(confusion.tp, confusion.tp + confusion.fp)
    recall = _safe_div(confusion.tp, confusion.tp + confusion.fn)
    f1 = _safe_div(2 * precision * recall, precision + recall)
    kappa = _cohen_kappa(confusion, accuracy)

    return PilotMetrics(
        accuracy=accuracy,
        precision=precision,
        recall=recall,
        f1=f1,
        kappa=kappa,
        confusion=confusion,
    )


# Alias matching the issue / plan name for the metrics bundle.
PilotStatistics = PilotMetrics


def _cohen_kappa(confusion: Confusion, observed_agreement: float) -> float:
    n = confusion.n
    actual_pos = confusion.tp + confusion.fn
    actual_neg = confusion.tn + confusion.fp
    pred_pos = confusion.tp + confusion.fp
    pred_neg = confusion.tn + confusion.fn
    expected = (actual_pos * pred_pos + actual_neg * pred_neg) / (n * n)
    denom = 1.0 - expected
    if denom == 0.0:
        return 1.0 if observed_agreement == 1.0 else 0.0
    return (observed_agreement - expected) / denom


def _safe_div(numerator: float, denominator: float) -> float:
    if denominator == 0.0:
        return 0.0
    return numerator / denominator


def _require_aligned(y_true: Sequence[bool], y_pred: Sequence[bool]) -> None:
    if len(y_true) != len(y_pred):
        raise ValueError(
            f"y_true and y_pred length mismatch: {len(y_true)} != {len(y_pred)}"
        )
