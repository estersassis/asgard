"""Binary decision: does this pull request contain a security discussion?"""

import re
from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from asgard.core.events import ClassificationAttempt, Event
from asgard.core.normalization import NormalizedPullRequest
from asgard.prompts.classifier import build_messages
from asgard.providers.factory import LLMFactory

_LOCATION = re.compile(
    r"description|general_discussion\[\d+\]|code_review_threads\[\d+\]\.comments\[\d+\]"
)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quote: str = Field(min_length=1)
    location: str

    @field_validator("location")
    @classmethod
    def _location(cls, value: str) -> str:
        if _LOCATION.fullmatch(value) is None:
            raise ValueError(
                "location must be description, general_discussion[i], "
                "or code_review_threads[j].comments[k]"
            )
        return value


class Classification(BaseModel):
    model_config = ConfigDict(extra="forbid")

    has_security_discussion: bool
    evidence: list[Evidence]
    rationale: str

    @model_validator(mode="after")
    def _evidence_matches_decision(self) -> "Classification":
        if self.has_security_discussion and not self.evidence:
            raise ValueError("evidence is required when has_security_discussion is true")
        if not self.has_security_discussion and self.evidence:
            raise ValueError("evidence must be empty when has_security_discussion is false")
        return self


def classify(
    pull: NormalizedPullRequest,
    factory: LLMFactory,
    *,
    emit: Callable[[Event], None],
    previous: str | None = None,
    feedback: str | None = None,
) -> Classification:
    """Classify one pull request. Emit an attempt only after a valid result."""
    messages = build_messages(pull, previous=previous, feedback=feedback)
    response = factory.generate("classifier", messages, schema=Classification)
    result = response.parsed
    if not isinstance(result, Classification):
        raise TypeError("classifier response was not a Classification")
    emit(ClassificationAttempt(pr_id=pull.context.pr_id))
    return result
