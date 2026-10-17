"""Progress events emitted by stages. Sinks decide how to present them."""

from typing import Literal

from pydantic import BaseModel


class Event(BaseModel):
    type: str


class StageStarted(Event):
    type: Literal["StageStarted"] = "StageStarted"
    stage: str
    total: int | None = None


class StageFinished(Event):
    type: Literal["StageFinished"] = "StageFinished"
    stage: str
    skipped: bool = False


class PRMined(Event):
    type: Literal["PRMined"] = "PRMined"
    pr_id: str


class RateLimited(Event):
    type: Literal["RateLimited"] = "RateLimited"
    wait_seconds: float


class PRFiltered(Event):
    type: Literal["PRFiltered"] = "PRFiltered"
    pr_id: str
    reason: str


class ClassifyProgress(Event):
    """Where classify is in the run: one batch, or one pull request."""

    type: Literal["ClassifyProgress"] = "ClassifyProgress"
    kind: Literal["batch", "pull"]
    index: int
    total: int


class ClassificationAttempt(Event):
    type: Literal["ClassificationAttempt"] = "ClassificationAttempt"
    pr_id: str
    has_security_discussion: bool | None = None
    quote: str | None = None


class EvaluatorFinding(BaseModel):
    criterion: str
    passed: bool
    explanation: str = ""


class EvaluatorVerdict(Event):
    type: Literal["EvaluatorVerdict"] = "EvaluatorVerdict"
    pr_id: str
    approved: bool
    findings: list[EvaluatorFinding] = []
    feedback: str = ""


class LoopFinished(Event):
    type: Literal["LoopFinished"] = "LoopFinished"
    pr_id: str


class SynthesisChunk(Event):
    type: Literal["SynthesisChunk"] = "SynthesisChunk"
    text: str


class CitationWarning(Event):
    type: Literal["CitationWarning"] = "CitationWarning"
    pr_id: str
    message: str


class Error(Event):
    type: Literal["Error"] = "Error"
    message: str
    stage: str | None = None
