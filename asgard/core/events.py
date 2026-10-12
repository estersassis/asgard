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


class PRFiltered(Event):
    type: Literal["PRFiltered"] = "PRFiltered"
    pr_id: str
    reason: str


class ClassificationAttempt(Event):
    type: Literal["ClassificationAttempt"] = "ClassificationAttempt"
    pr_id: str


class EvaluatorVerdict(Event):
    type: Literal["EvaluatorVerdict"] = "EvaluatorVerdict"
    pr_id: str
    approved: bool


class LoopFinished(Event):
    type: Literal["LoopFinished"] = "LoopFinished"
    pr_id: str


class SynthesisChunk(Event):
    type: Literal["SynthesisChunk"] = "SynthesisChunk"
    text: str


class Error(Event):
    type: Literal["Error"] = "Error"
    message: str
    stage: str | None = None
