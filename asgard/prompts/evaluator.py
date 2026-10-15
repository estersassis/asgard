"""Review a binary security-discussion classification."""

import json
from collections.abc import Sequence

from asgard.core.classification import Classification
from asgard.core.normalization import NormalizedPullRequest
from asgard.providers.models import Message

SYSTEM_PROMPT = """\
You review one classification of a pull request.

The classification says whether the pull request contains a security discussion.
Judge that answer. Do not write a new classification.

Judge these four criteria, in this order:
- decision: is has_security_discussion correct for this pull request?
- evidence: do the quotes show a security discussion, or were they taken out of context?
- completeness: when has_security_discussion is false, did the classifier miss a
  security discussion?
- rationale: is the rationale compatible with the evidence, and does it avoid inventing facts?

Return JSON with exactly these fields:
- verdict: APPROVE or REVISE
- findings: four objects, one for each criterion, in this order:
  decision, evidence, completeness, rationale
- feedback: guidance for the classifier

Each finding has:
- criterion: decision, evidence, completeness, or rationale
- passed: true or false
- explanation: a non-empty justification

APPROVE is valid only when every finding passed.
REVISE requires at least one finding with passed false, and feedback must be non-empty.
When the verdict is APPROVE, feedback is an empty string.
"""


def build_messages(
    pull: NormalizedPullRequest,
    classification: Classification,
) -> list[Message]:
    """Return the system prompt and one user message for this review.

    The user message contains the pull request JSON and the classification JSON.
    """
    content = (
        "Pull request:\n"
        f"{pull.model_dump_json(indent=2)}\n\n"
        "Classification:\n"
        f"{classification.model_dump_json(indent=2)}"
    )
    return [
        Message(role="system", content=SYSTEM_PROMPT),
        Message(role="user", content=content),
    ]


BATCH_SYSTEM_PROMPT = """\
You review classifications of several pull requests.

Each classification says whether that pull request contains a security discussion.
Judge each answer on its own. Do not write a new classification.
Do not use one pull request, or one classification, when judging another.

Judge these four criteria, in this order, for each pull request:
- decision: is has_security_discussion correct for this pull request?
- evidence: do the quotes show a security discussion, or were they taken out of context?
- completeness: when has_security_discussion is false, did the classifier miss a
  security discussion?
- rationale: is the rationale compatible with the evidence, and does it avoid inventing facts?

Return JSON with exactly this field:
- results: one object per pull request, each with pr_id, verdict, findings,
  and feedback

pr_id is the id of that pull request.
verdict is APPROVE or REVISE.
findings has four objects, one for each criterion, in this order:
decision, evidence, completeness, rationale.
feedback is guidance for the classifier about this pull request only.

Each finding has:
- criterion: decision, evidence, completeness, or rationale
- passed: true or false
- explanation: a non-empty justification

APPROVE is valid only when every finding passed.
REVISE requires at least one finding with passed false, and feedback must be non-empty.
When the verdict is APPROVE, feedback is an empty string.
"""


def build_batch_messages(
    items: Sequence[tuple[NormalizedPullRequest, Classification]],
) -> list[Message]:
    """Return the batch system prompt and one user message for these pairs.

    Each item is one pull request and the classification to review.
    Pairs are judged independently.
    """
    if not items:
        raise ValueError("a batch requires at least one pull request")

    payload = [
        {
            "pr_id": pull.context.pr_id,
            "pull_request": pull.model_dump(mode="json"),
            "classification": classification.model_dump(mode="json"),
        }
        for pull, classification in items
    ]
    content = (
        "Judge each pair independently. "
        "description, general_discussion[i], and code_review_threads[j].comments[k] "
        "belong only to the pull request in that pair.\n\n"
        f"{json.dumps(payload, indent=2)}"
    )
    return [
        Message(role="system", content=BATCH_SYSTEM_PROMPT),
        Message(role="user", content=content),
    ]
