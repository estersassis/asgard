"""Binary decision: does this pull request contain a security discussion?"""

import re
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from asgard.core.events import ClassificationAttempt, Event
from asgard.core.normalization import NormalizedPullRequest
from asgard.prompts.classifier import build_messages
from asgard.providers.factory import LLMFactory

_LOCATION = re.compile(
    r"title|description|general_discussion\[\d+\]|code_review_threads\[\d+\]\.comments\[\d+\]"
)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote: str = Field(min_length=1)
    location: str

    @field_validator("location")
    @classmethod
    def _location(cls, value: str) -> str:
        if value == "context.title":
            value = "title"
        if _LOCATION.fullmatch(value) is None:
            raise ValueError(
                "location must be title, description, general_discussion[i], "
                "or code_review_threads[j].comments[k]"
            )
        return value


def _coerce_evidence_to_decision(data: Any) -> Any:
    """Drop evidence when the model says false but still quotes text."""
    if not isinstance(data, dict):
        return data
    if data.get("has_security_discussion") is not False:
        return data
    evidence = data.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        return data
    return {**data, "evidence": []}


def _evidence_matches_decision(has_security_discussion: bool, evidence: list[Evidence]) -> None:
    if has_security_discussion and not evidence:
        raise ValueError("evidence is required when has_security_discussion is true")
    if not has_security_discussion and evidence:
        raise ValueError("evidence must be empty when has_security_discussion is false")


class Classification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    has_security_discussion: bool
    evidence: list[Evidence]
    rationale: str

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        return _coerce_evidence_to_decision(data)

    @model_validator(mode="after")
    def _check_evidence(self) -> "Classification":
        _evidence_matches_decision(self.has_security_discussion, self.evidence)
        return self


class BatchClassificationItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pr_id: str
    has_security_discussion: bool
    evidence: list[Evidence]
    rationale: str

    @model_validator(mode="before")
    @classmethod
    def _coerce(cls, data: Any) -> Any:
        return _coerce_evidence_to_decision(data)

    @model_validator(mode="after")
    def _check_evidence(self) -> "BatchClassificationItem":
        _evidence_matches_decision(self.has_security_discussion, self.evidence)
        return self


class BatchClassification(BaseModel):
    """One classifier response covering every pull request in the call."""

    model_config = ConfigDict(extra="forbid")

    results: list[BatchClassificationItem]


def classify(
    pull: NormalizedPullRequest,
    factory: LLMFactory,
    *,
    emit: Callable[[Event], None],
    previous: str | None = None,
    feedback: str | None = None,
    system_prompt: str | None = None,
) -> Classification:
    """Classify one pull request. Emit an attempt only after a valid result."""
    messages = build_messages(
        pull,
        previous=previous,
        feedback=feedback,
        system_prompt=system_prompt,
    )
    response = factory.generate("classifier", messages, schema=Classification)
    result = response.parsed
    if not isinstance(result, Classification):
        raise TypeError("classifier response was not a Classification")
    quote = result.evidence[0].quote if result.evidence else None
    emit(
        ClassificationAttempt(
            pr_id=pull.context.pr_id,
            has_security_discussion=result.has_security_discussion,
            quote=quote,
        )
    )
    return result
