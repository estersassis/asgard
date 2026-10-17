"""Batch classifier and evaluator prompts. No network."""

import json

import pytest
from pydantic import ValidationError

from asgard.core.classification import BatchClassification, Classification, Evidence
from asgard.core.evaluation import BatchEvaluation
from asgard.core.normalization import NormalizedPullRequest, PullRequestContext
from asgard.prompts.classifier import (
    BATCH_SYSTEM_PROMPT as CLASSIFIER_BATCH_PROMPT,
)
from asgard.prompts.classifier import (
    SYSTEM_PROMPT as CLASSIFIER_PROMPT,
)
from asgard.prompts.classifier import (
    build_batch_messages as build_classifier_batch,
)
from asgard.prompts.classifier import (
    build_messages as build_classifier_messages,
)
from asgard.prompts.evaluator import (
    BATCH_SYSTEM_PROMPT as EVALUATOR_BATCH_PROMPT,
)
from asgard.prompts.evaluator import (
    SYSTEM_PROMPT as EVALUATOR_PROMPT,
)
from asgard.prompts.evaluator import (
    build_batch_messages as build_evaluator_batch,
)
from asgard.prompts.evaluator import (
    build_messages as build_evaluator_messages,
)

_CRITERIA = ("decision", "evidence", "completeness", "rationale")


def _pull(pr_id: str, description: str) -> NormalizedPullRequest:
    return NormalizedPullRequest(
        context=PullRequestContext(
            repository="acme/app",
            pr_id=pr_id,
            title=f"Change {pr_id}",
            state="open",
            is_merged=False,
        ),
        description=description,
        general_discussion=[f"comment on {pr_id}"],
        code_review_threads=[],
    )


def _classification() -> Classification:
    return Classification(
        has_security_discussion=True,
        evidence=[Evidence(quote="authentication bypass", location="description")],
        rationale="The description states an authentication bypass.",
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


def test_single_pr_prompts_stay_on_their_own_system_text() -> None:
    pull = _pull("42", "The login handler allows an authentication bypass.")
    classification = _classification()

    classifier = build_classifier_messages(pull)
    evaluator = build_evaluator_messages(pull, classification)

    assert classifier[0].content == CLASSIFIER_PROMPT
    assert evaluator[0].content == EVALUATOR_PROMPT
    assert "results" not in CLASSIFIER_PROMPT
    assert "results" not in EVALUATOR_PROMPT
    assert classifier[0].content != CLASSIFIER_BATCH_PROMPT
    assert evaluator[0].content != EVALUATOR_BATCH_PROMPT


def test_classifier_batch_asks_for_an_independent_judgment_per_pr() -> None:
    first = _pull("42", "The login handler allows an authentication bypass.")
    second = _pull("7", "Rename the button label.")

    messages = build_classifier_batch([(first, None, None), (second, None, None)])

    assert messages[0].content == CLASSIFIER_BATCH_PROMPT
    user = messages[1].content
    assert "Judge each pull request independently" in user
    assert (
        "title, description, general_discussion[i], and "
        "code_review_threads[j].comments[k]"
    ) in user
    assert "belong only to that pull request" in user
    payload = json.loads(user.split("\n\n", 1)[1])
    assert [item["pr_id"] for item in payload] == ["42", "7"]
    assert payload[0]["pull_request"] == first.model_dump(mode="json")
    assert payload[1]["pull_request"] == second.model_dump(mode="json")
    assert "previous_response" not in payload[0]
    assert "evaluator_feedback" not in payload[0]


def test_classifier_batch_keeps_feedback_on_its_own_pull_request() -> None:
    first = _pull("42", "The login handler allows an authentication bypass.")
    second = _pull("7", "Rename the button label.")
    previous = '{"has_security_discussion": false}'
    feedback = "The description of 42 states an authentication bypass."

    messages = build_classifier_batch(
        [(first, previous, feedback), (second, None, None)]
    )

    payload = json.loads(messages[1].content.split("\n\n", 1)[1])
    assert payload[0]["previous_response"] == previous
    assert payload[0]["evaluator_feedback"] == feedback
    assert "previous_response" not in payload[1]
    assert "evaluator_feedback" not in payload[1]
    assert feedback not in json.dumps(payload[1])


def test_classifier_batch_requires_previous_and_feedback_together() -> None:
    pull = _pull("42", "The login handler allows an authentication bypass.")

    with pytest.raises(ValueError, match="previous and feedback"):
        build_classifier_batch([(pull, "previous", None)])
    with pytest.raises(ValueError, match="at least one"):
        build_classifier_batch([])


def test_classifier_batch_schema_uses_the_single_pr_evidence_rules() -> None:
    accepted = BatchClassification.model_validate(
        {
            "results": [
                {
                    "pr_id": "42",
                    "has_security_discussion": True,
                    "evidence": [
                        {"quote": "authentication bypass", "location": "description"}
                    ],
                    "rationale": "The description states an authentication bypass.",
                },
                {
                    "pr_id": "7",
                    "has_security_discussion": False,
                    "evidence": [],
                    "rationale": "The change does not discuss security.",
                },
            ]
        }
    )
    assert [item.pr_id for item in accepted.results] == ["42", "7"]

    with pytest.raises(ValidationError, match="evidence is required"):
        BatchClassification.model_validate(
            {
                "results": [
                    {
                        "pr_id": "42",
                        "has_security_discussion": True,
                        "evidence": [],
                        "rationale": "claimed without a quote",
                    }
                ]
            }
        )
    with pytest.raises(ValidationError, match="evidence must be empty"):
        BatchClassification.model_validate(
            {
                "results": [
                    {
                        "pr_id": "7",
                        "has_security_discussion": False,
                        "evidence": [
                            {"quote": "authentication bypass", "location": "description"}
                        ],
                        "rationale": "no security discussion",
                    }
                ]
            }
        )
    with pytest.raises(ValidationError):
        BatchClassification.model_validate(
            {
                "results": [
                    {
                        "pr_id": "42",
                        "has_security_discussion": True,
                        "evidence": [{"quote": "authentication bypass", "location": "body"}],
                        "rationale": "bad location",
                    }
                ]
            }
        )
    with pytest.raises(ValidationError):
        BatchClassification.model_validate({"results": [], "note": "extra"})


def test_evaluator_batch_sends_each_pair_and_asks_for_independence() -> None:
    first = _pull("42", "The login handler allows an authentication bypass.")
    second = _pull("7", "Rename the button label.")
    first_classification = _classification()
    second_classification = Classification(
        has_security_discussion=False,
        evidence=[],
        rationale="The change does not discuss security.",
    )

    messages = build_evaluator_batch(
        [(first, first_classification), (second, second_classification)]
    )

    assert messages[0].content == EVALUATOR_BATCH_PROMPT
    user = messages[1].content
    assert "Judge each pair independently" in user
    assert (
        "title, description, general_discussion[i], and "
        "code_review_threads[j].comments[k]"
    ) in user
    assert "belong only to the pull request in that pair" in user
    payload = json.loads(user.split("\n\n", 1)[1])
    assert payload[0]["pr_id"] == "42"
    assert payload[0]["pull_request"] == first.model_dump(mode="json")
    assert payload[0]["classification"] == first_classification.model_dump(mode="json")
    assert payload[1]["classification"] == second_classification.model_dump(mode="json")


def test_evaluator_batch_schema_uses_the_single_pr_verdict_rules() -> None:
    accepted = BatchEvaluation.model_validate(
        {
            "results": [
                {
                    "pr_id": "42",
                    "verdict": "APPROVE",
                    "findings": _findings(),
                    "feedback": "",
                }
            ]
        }
    )
    assert accepted.results[0].verdict == "APPROVE"

    with pytest.raises(ValidationError, match="APPROVE requires every finding"):
        BatchEvaluation.model_validate(
            {
                "results": [
                    {
                        "pr_id": "42",
                        "verdict": "APPROVE",
                        "findings": _findings(failed="evidence"),
                        "feedback": "fix the quote",
                    }
                ]
            }
        )
    with pytest.raises(ValidationError, match="REVISE requires non-empty feedback"):
        BatchEvaluation.model_validate(
            {
                "results": [
                    {
                        "pr_id": "42",
                        "verdict": "REVISE",
                        "findings": _findings(failed="evidence"),
                        "feedback": "",
                    }
                ]
            }
        )
    with pytest.raises(ValidationError, match="at least one finding"):
        BatchEvaluation.model_validate(
            {
                "results": [
                    {
                        "pr_id": "42",
                        "verdict": "REVISE",
                        "findings": _findings(),
                        "feedback": "nothing failed",
                    }
                ]
            }
        )
    with pytest.raises(ValidationError):
        BatchEvaluation.model_validate(
            {
                "results": [
                    {
                        "pr_id": "42",
                        "verdict": "REVISE",
                        "findings": list(reversed(_findings(failed="decision"))),
                        "feedback": "wrong order",
                    }
                ]
            }
        )
    with pytest.raises(ValidationError):
        BatchEvaluation.model_validate({"results": [], "note": "extra"})


def test_evaluator_batch_rejects_an_empty_list() -> None:
    with pytest.raises(ValueError, match="at least one"):
        build_evaluator_batch([])
