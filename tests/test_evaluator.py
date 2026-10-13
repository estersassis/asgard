"""Evaluator contract. No network."""

import json

import pytest
from pydantic import ValidationError

from asgard.core.classification import Classification, Evidence
from asgard.core.evaluation import Evaluation, evaluate
from asgard.core.events import EvaluatorVerdict
from asgard.core.normalization import (
    CodeReviewThread,
    NormalizedPullRequest,
    PullRequestContext,
)
from asgard.providers.factory import LLMFactory
from asgard.providers.handlers.fake import FakeHandler
from asgard.providers.models import Credentials, LLMSettings, RoleSpec

DESCRIPTION = "The login handler allows an authentication bypass."
COMMENT = "Please rotate the exposed secret."
_CRITERIA = ("decision", "evidence", "completeness", "rationale")


def _pull() -> NormalizedPullRequest:
    return NormalizedPullRequest(
        context=PullRequestContext(
            repository="acme/app",
            pr_id="42",
            title="Fix authentication bypass",
            state="open",
            is_merged=False,
        ),
        description=DESCRIPTION,
        general_discussion=["Looks good to me."],
        code_review_threads=[CodeReviewThread(scope="src/auth.py", comments=[COMMENT])],
    )


def _classification(
    quote: str = "authentication bypass",
    location: str = "description",
) -> Classification:
    return Classification(
        has_security_discussion=True,
        evidence=[Evidence(quote=quote, location=location)],
        rationale="The description states an authentication bypass.",
    )


def _factory(handler: FakeHandler) -> LLMFactory:
    settings = LLMSettings(default=RoleSpec(provider="fake", model="fake"))
    return LLMFactory(
        settings,
        Credentials(),
        handlers={"fake": handler},
        sleep=lambda _: None,
    )


def _findings(*, failed: str | None = None) -> list[dict[str, object]]:
    return [
        {
            "criterion": criterion,
            "passed": criterion != failed,
            "explanation": f"{criterion} checked",
        }
        for criterion in _CRITERIA
    ]


def _payload(verdict: str, *, failed: str | None = None, feedback: str = "") -> str:
    return json.dumps(
        {
            "verdict": verdict,
            "findings": _findings(failed=failed),
            "feedback": feedback,
        }
    )


def test_fabricated_quote_revises_without_calling_the_model() -> None:
    handler = FakeHandler(["should not be used"])
    events: list[EvaluatorVerdict] = []
    quote = "this quote was invented"
    classification = Classification(
        has_security_discussion=True,
        evidence=[
            Evidence(quote="authentication bypass", location="description"),
            Evidence(quote=quote, location="description"),
        ],
        rationale="The description states an authentication bypass.",
    )

    result = evaluate(_pull(), classification, _factory(handler), emit=events.append)

    assert result.verdict == "REVISE"
    assert [finding.criterion for finding in result.findings] == list(_CRITERIA)
    evidence = result.findings[1]
    assert evidence.criterion == "evidence"
    assert evidence.passed is False
    assert quote in evidence.explanation
    assert "description" in evidence.explanation
    assert "authentication bypass" not in evidence.explanation
    assert all(finding.passed for finding in result.findings if finding.criterion != "evidence")
    assert all(
        finding.explanation == "LLM review did not run."
        for finding in result.findings
        if finding.criterion != "evidence"
    )
    assert "literal" in result.feedback
    assert quote in result.feedback
    assert events == [EvaluatorVerdict(pr_id="42", approved=False)]
    assert handler.calls == []


def test_invalid_location_revises_without_calling_the_model() -> None:
    handler = FakeHandler(["should not be used"])
    events: list[EvaluatorVerdict] = []
    classification = _classification(location="general_discussion[3]")

    result = evaluate(_pull(), classification, _factory(handler), emit=events.append)

    assert result.verdict == "REVISE"
    assert result.findings[1].passed is False
    assert "general_discussion[3]" in result.findings[1].explanation
    assert events == [EvaluatorVerdict(pr_id="42", approved=False)]
    assert handler.calls == []


def test_empty_evidence_revises_without_calling_the_model() -> None:
    handler = FakeHandler(["should not be used"])
    events: list[EvaluatorVerdict] = []
    classification = Classification.model_construct(
        has_security_discussion=True,
        evidence=[],
        rationale="claimed without a quote",
    )

    result = evaluate(_pull(), classification, _factory(handler), emit=events.append)

    assert result.verdict == "REVISE"
    assert result.findings[1].passed is False
    assert "empty" in result.findings[1].explanation
    assert events == [EvaluatorVerdict(pr_id="42", approved=False)]
    assert handler.calls == []


def test_approve_emits_verdict_and_sends_pr_and_classification() -> None:
    handler = FakeHandler([_payload("APPROVE")])
    events: list[EvaluatorVerdict] = []
    pull = _pull()
    classification = _classification()

    result = evaluate(pull, classification, _factory(handler), emit=events.append)

    assert result.verdict == "APPROVE"
    assert [finding.criterion for finding in result.findings] == list(_CRITERIA)
    assert all(finding.passed for finding in result.findings)
    assert events == [EvaluatorVerdict(pr_id="42", approved=True)]
    user = handler.calls[0][1].content
    assert pull.model_dump_json(indent=2) in user
    assert classification.model_dump_json(indent=2) in user


def test_model_revise_keeps_feedback_and_rejects() -> None:
    feedback = "The quote does not show a security discussion."
    handler = FakeHandler([_payload("REVISE", failed="decision", feedback=feedback)])
    events: list[EvaluatorVerdict] = []

    result = evaluate(_pull(), _classification(), _factory(handler), emit=events.append)

    assert result.verdict == "REVISE"
    assert result.feedback == feedback
    assert result.findings[0].passed is False
    assert events == [EvaluatorVerdict(pr_id="42", approved=False)]


def test_invalid_json_on_both_attempts_raises_without_event() -> None:
    handler = FakeHandler(["not json", "still not json"])
    events: list[EvaluatorVerdict] = []

    with pytest.raises(ValueError, match="response is not JSON"):
        evaluate(_pull(), _classification(), _factory(handler), emit=events.append)

    assert events == []
    assert len(handler.calls) == 2


def test_incoherent_verdict_fails_validation() -> None:
    with pytest.raises(ValidationError):
        Evaluation.model_validate(
            {
                "verdict": "APPROVE",
                "findings": _findings(failed="evidence"),
                "feedback": "fix the quote",
            }
        )
    with pytest.raises(ValidationError):
        Evaluation.model_validate(
            {
                "verdict": "REVISE",
                "findings": _findings(failed="evidence"),
                "feedback": "",
            }
        )
    with pytest.raises(ValidationError):
        Evaluation.model_validate(
            {
                "verdict": "REVISE",
                "findings": _findings(),
                "feedback": "nothing failed",
            }
        )
