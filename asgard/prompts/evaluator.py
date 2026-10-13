"""Review a binary security-discussion classification."""

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
