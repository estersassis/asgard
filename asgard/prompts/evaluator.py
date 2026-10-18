"""Review a binary security-discussion classification."""

import json
from collections.abc import Sequence

from asgard.core.classification import Classification
from asgard.core.normalization import NormalizedPullRequest
from asgard.providers.models import Message

SYSTEM_PROMPT = """\
You review one classification of a pull request. Judge it; do not reclassify.

Use the classifier's definition: a security discussion exists only when the pull
request's text states a security risk or a protection against one (a vulnerability
or attack, an access control or authentication flaw, exposed secrets, unsafe input
handling, an insecure default, or a dependency update made for a security reason).
Security-related words or components without a stated risk do not count.

Check these criteria in order:
1. decision: is has_security_discussion correct under that definition?
2. evidence: if true, does each quote state the risk when read in its context?
   A lone keyword fails. If false, this passes when evidence is [].
3. completeness: if false, does the title, description, discussion or any review
   comment state a risk the classifier missed? If true, this passes.
4. rationale: does it match the evidence and the text without inventing facts?

Fail a criterion only for a concrete problem you can point to in the text.
Doubt alone is not a failure.

Return JSON with exactly: verdict (APPROVE or REVISE), findings (four objects in
the order above, each with criterion, passed, explanation), and feedback.
APPROVE only when all four pass, with feedback "". Otherwise REVISE, with feedback
that names what to fix and where (location). If the decision is wrong, feedback
must start with "The decision is wrong:".
"""


def build_messages(
    pull: NormalizedPullRequest,
    classification: Classification,
    system_prompt: str | None = None,
) -> list[Message]:
    """Return the system prompt and one user message for this review.

    The user message contains the pull request JSON and the classification JSON.
    ``system_prompt`` overrides ``SYSTEM_PROMPT`` when set.
    """
    content = (
        "Pull request:\n"
        f"{pull.model_dump_json(indent=2)}\n\n"
        "Classification:\n"
        f"{classification.model_dump_json(indent=2)}"
    )
    prompt = SYSTEM_PROMPT if system_prompt is None else system_prompt
    return [
        Message(role="system", content=prompt),
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
        "title, description, general_discussion[i], and "
        "code_review_threads[j].comments[k] belong only to the pull request in that pair.\n\n"
        f"{json.dumps(payload, indent=2)}"
    )
    return [
        Message(role="system", content=BATCH_SYSTEM_PROMPT),
        Message(role="user", content=content),
    ]
