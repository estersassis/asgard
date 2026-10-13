"""Binary classifier contract. No network."""

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from asgard.core.classification import Classification, classify
from asgard.core.events import ClassificationAttempt
from asgard.core.normalization import NormalizedPullRequest, PullRequestContext
from asgard.providers.factory import LLMFactory
from asgard.providers.handlers.fake import FakeHandler
from asgard.providers.models import Credentials, LLMSettings, RoleSpec

PROMPT = Path(__file__).resolve().parents[1] / "asgard" / "prompts" / "classifier.py"
DESCRIPTION = "The login handler allows an authentication bypass."


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
        code_review_threads=[],
    )


def _factory(handler: FakeHandler) -> LLMFactory:
    settings = LLMSettings(default=RoleSpec(provider="fake", model="fake"))
    return LLMFactory(
        settings,
        Credentials(),
        handlers={"fake": handler},
        sleep=lambda _: None,
    )


def _payload(has_security_discussion: bool) -> str:
    if has_security_discussion:
        body = {
            "has_security_discussion": True,
            "evidence": [{"quote": "authentication bypass", "location": "description"}],
            "rationale": "The description states an authentication bypass.",
        }
    else:
        body = {
            "has_security_discussion": False,
            "evidence": [],
            "rationale": "The change does not discuss security.",
        }
    return json.dumps(body)


def test_true_decision_emits_attempt_and_sends_pr_text() -> None:
    handler = FakeHandler([_payload(True)])
    events: list[ClassificationAttempt] = []
    pull = _pull()

    result = classify(pull, _factory(handler), emit=events.append)

    assert result.has_security_discussion is True
    assert result.evidence[0].quote == "authentication bypass"
    assert result.evidence[0].location == "description"
    assert result.rationale == "The description states an authentication bypass."
    assert events == [ClassificationAttempt(pr_id="42")]
    user = handler.calls[0][1].content
    assert DESCRIPTION in user
    assert pull.context.title in user


def test_false_decision_has_empty_evidence() -> None:
    handler = FakeHandler([_payload(False)])
    events: list[ClassificationAttempt] = []

    result = classify(_pull(), _factory(handler), emit=events.append)

    assert result.has_security_discussion is False
    assert result.evidence == []
    assert events == [ClassificationAttempt(pr_id="42")]


def test_invalid_json_on_both_attempts_raises_without_event() -> None:
    handler = FakeHandler(["not json", "still not json"])
    events: list[ClassificationAttempt] = []

    with pytest.raises(ValueError, match="response is not JSON"):
        classify(_pull(), _factory(handler), emit=events.append)

    assert events == []
    assert len(handler.calls) == 2


def test_retry_message_includes_previous_response_and_feedback() -> None:
    handler = FakeHandler([_payload(True)])
    events: list[ClassificationAttempt] = []
    previous = '{"has_security_discussion": false, "evidence": [], "rationale": "ui"}'
    feedback = "The description states an authentication bypass."

    result = classify(
        _pull(),
        _factory(handler),
        emit=events.append,
        previous=previous,
        feedback=feedback,
    )

    assert result.has_security_discussion is True
    user = handler.calls[0][1].content
    assert previous in user
    assert feedback in user
    assert events == [ClassificationAttempt(pr_id="42")]


def test_prompt_source_has_no_owasp_or_nature_labels() -> None:
    source = PROMPT.read_text(encoding="utf-8")
    for term in (
        "OWASP",
        "owasp_category",
        "FIX/PREVENTION",
        "VULNERABILITY_INTRODUCTION",
    ):
        assert term not in source


def test_incoherent_evidence_fails_validation() -> None:
    with pytest.raises(ValidationError):
        Classification.model_validate(
            {
                "has_security_discussion": True,
                "evidence": [],
                "rationale": "claimed without a quote",
            }
        )
    with pytest.raises(ValidationError):
        Classification.model_validate(
            {
                "has_security_discussion": False,
                "evidence": [{"quote": "authentication bypass", "location": "description"}],
                "rationale": "no security discussion",
            }
        )
