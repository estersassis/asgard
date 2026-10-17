"""Pilot-set validation against classifier prompts."""

from asgard.core.pilot.gate import (
    GateResult,
    GateThresholds,
    PromoteError,
    evaluate_gates,
    promote,
)
from asgard.core.pilot.runner import PilotPrediction, PilotResult, run_pilot
from asgard.core.pilot.set import PilotItem, PilotSet, load_pilot_set
from asgard.core.pilot.statistics import (
    Confusion,
    PilotMetrics,
    PilotStatistics,
    compute_metrics,
    confusion_matrix,
)

__all__ = [
    "Confusion",
    "GateResult",
    "GateThresholds",
    "PilotItem",
    "PilotMetrics",
    "PilotPrediction",
    "PilotResult",
    "PilotSet",
    "PilotStatistics",
    "PromoteError",
    "compute_metrics",
    "confusion_matrix",
    "evaluate_gates",
    "load_pilot_set",
    "promote",
    "run_pilot",
]
