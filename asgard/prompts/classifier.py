"""Binary prompt: does this pull request contain a security discussion?"""

from asgard.core.normalization import NormalizedPullRequest
from asgard.providers.models import Message

SYSTEM_PROMPT = """\
You decide whether one pull request contains a security discussion.

The decision is true only when the pull request text itself supports it.
Do not invent a security problem the text does not state.

Count it as a security discussion when the text addresses any of these:
- a vulnerability
- access control
- authentication
- secrets
- input validation
- insecure defaults
- a dependency update made for a security reason

Do not count it when the text is only:
- the word "secure" used in passing
- maintenance
- user interface work
- performance
- a bug with no security impact
- a refactor that does not discuss security

Read the whole pull request, including its title and description.
A quote taken from the description is valid evidence.

Each quote is a non-empty literal excerpt copied from the cited place.
Use one of these location forms, with zero-based indexes:
- description
- general_discussion[i]
- code_review_threads[j].comments[k]

Return JSON with exactly these fields:
- has_security_discussion: true or false
- evidence: a list of objects, each with quote and location
- rationale: a short justification grounded in the pull request text

When has_security_discussion is true, evidence has at least one item.
When has_security_discussion is false, evidence is an empty list.

When a previous response and evaluator feedback follow the pull request,
revise the decision using that feedback. The feedback is about the last
answer. Do not treat it as pull request text.
"""


def build_messages(
    pull: NormalizedPullRequest,
    previous: str | None = None,
    feedback: str | None = None,
) -> list[Message]:
    """Return the system prompt and one user message for this pull request.

    ``previous`` and ``feedback`` are included together on a later attempt.
    ``feedback`` is opaque evaluator text.
    """
    if (previous is None) != (feedback is None):
        raise ValueError("previous and feedback must both be set or both be omitted")

    content = pull.model_dump_json(indent=2)
    if previous is not None and feedback is not None:
        content = (
            f"{content}\n\n"
            f"Previous response:\n{previous}\n\n"
            f"Evaluator feedback:\n{feedback}"
        )
    return [
        Message(role="system", content=SYSTEM_PROMPT),
        Message(role="user", content=content),
    ]
