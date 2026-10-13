"""Review a classification against the pull request text."""

import re
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from asgard.core.classification import Classification
from asgard.core.events import EvaluatorVerdict, Event
from asgard.core.normalization import NormalizedPullRequest
from asgard.prompts.evaluator import build_messages
from asgard.providers.factory import LLMFactory

Criterion = Literal["decision", "evidence", "completeness", "rationale"]
Verdict = Literal["APPROVE", "REVISE"]

_CRITERIA: tuple[Criterion, ...] = (
    "decision",
    "evidence",
    "completeness",
    "rationale",
)
_GENERAL = re.compile(r"general_discussion\[(\d+)\]")
_REVIEW = re.compile(r"code_review_threads\[(\d+)\]\.comments\[(\d+)\]")
_SKIPPED = "LLM review did not run."
_INSTRUCTION = "Quote a literal excerpt from the indicated location."


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion: Criterion
    passed: bool
    explanation: str = Field(min_length=1)


class Evaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    verdict: Verdict
    findings: list[Finding]
    feedback: str

    @model_validator(mode="after")
    def _consistent(self) -> "Evaluation":
        criteria = [finding.criterion for finding in self.findings]
        if criteria != list(_CRITERIA):
            raise ValueError(
                "findings must be decision, evidence, completeness, and rationale, in that order"
            )
        all_passed = all(finding.passed for finding in self.findings)
        if self.verdict == "APPROVE" and not all_passed:
            raise ValueError("APPROVE requires every finding to pass")
        if self.verdict == "REVISE" and all_passed:
            raise ValueError("REVISE requires at least one finding with passed false")
        if self.verdict == "REVISE" and not self.feedback:
            raise ValueError("REVISE requires non-empty feedback")
        return self


def evaluate(
    pull: NormalizedPullRequest,
    classification: Classification,
    factory: LLMFactory,
    *,
    emit: Callable[[Event], None],
) -> Evaluation:
    """Review one classification. Emit a verdict only after a result exists."""
    checked = _quote_revision(pull, classification)
    if checked is not None:
        emit(EvaluatorVerdict(pr_id=pull.context.pr_id, approved=False))
        return checked
    messages = build_messages(pull, classification)
    response = factory.generate("evaluator", messages, schema=Evaluation)
    result = response.parsed
    if not isinstance(result, Evaluation):
        raise TypeError("evaluator response was not an Evaluation")
    emit(
        EvaluatorVerdict(
            pr_id=pull.context.pr_id,
            approved=result.verdict == "APPROVE",
        )
    )
    return result


def _quote_revision(
    pull: NormalizedPullRequest,
    classification: Classification,
) -> Evaluation | None:
    problems = _evidence_problems(pull, classification)
    if not problems:
        return None
    explanation = " ".join(problems)
    return Evaluation(
        verdict="REVISE",
        findings=[
            Finding(
                criterion=criterion,
                passed=criterion != "evidence",
                explanation=explanation if criterion == "evidence" else _SKIPPED,
            )
            for criterion in _CRITERIA
        ],
        feedback=f"{_INSTRUCTION} {explanation}",
    )


def _evidence_problems(pull: NormalizedPullRequest, classification: Classification) -> list[str]:
    problems: list[str] = []
    if classification.has_security_discussion and not classification.evidence:
        problems.append("evidence is empty while has_security_discussion is true")
    for item in classification.evidence:
        text = _location_text(pull, item.location)
        if text is None:
            problems.append(f"invalid location {item.location}")
            continue
        if item.quote not in text:
            problems.append(f"missing quote {item.quote!r} at {item.location}")
    return problems


def _location_text(pull: NormalizedPullRequest, location: str) -> str | None:
    if location == "description":
        return pull.description
    general = _GENERAL.fullmatch(location)
    if general:
        index = int(general.group(1))
        if index >= len(pull.general_discussion):
            return None
        return pull.general_discussion[index]
    review = _REVIEW.fullmatch(location)
    if review:
        thread_index = int(review.group(1))
        comment_index = int(review.group(2))
        if thread_index >= len(pull.code_review_threads):
            return None
        comments = pull.code_review_threads[thread_index].comments
        if comment_index >= len(comments):
            return None
        return comments[comment_index]
    return None
