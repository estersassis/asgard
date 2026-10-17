"""Binary prompt: does this pull request contain a security discussion?"""

import json
from collections.abc import Sequence

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
A quote taken from the title or description is valid evidence.

Each quote is a non-empty literal excerpt copied from the cited place.
Use one of these location forms, with zero-based indexes:
- title
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
revise that answer using the feedback. The feedback is about the last
answer. Do not treat it as pull request text.

Keep has_security_discussion when the feedback is about evidence, quotes,
or locations. Change the decision only when the feedback says it is wrong.
When quotes fail, copy a non-empty literal excerpt from the cited place, or
fix the location so it points to where that excerpt actually appears.
Do not repeat the previous evidence unchanged when the feedback rejects it.
"""


def build_messages(
    pull: NormalizedPullRequest,
    previous: str | None = None,
    feedback: str | None = None,
    system_prompt: str | None = None,
) -> list[Message]:
    """Return the system prompt and one user message for this pull request.

    ``previous`` and ``feedback`` are included together on a later attempt.
    ``feedback`` is opaque evaluator text.
    ``system_prompt`` overrides ``SYSTEM_PROMPT`` when set.
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
    prompt = SYSTEM_PROMPT if system_prompt is None else system_prompt
    return [
        Message(role="system", content=prompt),
        Message(role="user", content=content),
    ]


BATCH_SYSTEM_PROMPT = """\
You decide whether each pull request in the list contains a security discussion.

Judge each pull request independently. Do not use text from one pull request
as evidence for another.

The decision is true only when that pull request's text itself supports it.
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
A quote taken from the title or description is valid evidence.

Each quote is a non-empty literal excerpt copied from the cited place in that
same pull request. Use one of these location forms, with zero-based indexes
that start again for each pull request:
- title
- description
- general_discussion[i]
- code_review_threads[j].comments[k]

Return JSON with exactly this field:
- results: one object per pull request, each with pr_id,
  has_security_discussion, evidence, and rationale

pr_id is the id of that pull request.
has_security_discussion is true or false.
evidence is a list of objects, each with quote and location.
rationale is a short justification grounded in that pull request's text.

When has_security_discussion is true, evidence has at least one item.
When has_security_discussion is false, evidence is an empty list.

When an item includes a previous response and evaluator feedback, revise only
that pull request using that feedback. The feedback is about the last answer
for that pull request. Do not treat it as pull request text, and do not apply
it to any other pull request.

Keep has_security_discussion when the feedback is about evidence, quotes,
or locations. Change the decision only when the feedback says it is wrong.
When quotes fail, copy a non-empty literal excerpt from the cited place in
that same pull request, or fix the location so it points to where that
excerpt actually appears. Do not repeat the previous evidence unchanged when
the feedback rejects it.
"""


def build_batch_messages(
    items: Sequence[tuple[NormalizedPullRequest, str | None, str | None]],
) -> list[Message]:
    """Return the batch system prompt and one user message for these pull requests.

    Each item is a pull request plus, on a later attempt, that pull request's
    previous response and evaluator feedback. ``previous`` and ``feedback``
    are included together or omitted together. Feedback applies only to the
    pull request it is attached to.
    """
    if not items:
        raise ValueError("a batch requires at least one pull request")

    payload: list[dict[str, object]] = []
    for pull, previous, feedback in items:
        if (previous is None) != (feedback is None):
            raise ValueError("previous and feedback must both be set or both be omitted")
        entry: dict[str, object] = {
            "pr_id": pull.context.pr_id,
            "pull_request": pull.model_dump(mode="json"),
        }
        if previous is not None and feedback is not None:
            entry["previous_response"] = previous
            entry["evaluator_feedback"] = feedback
        payload.append(entry)
    content = (
        "Judge each pull request independently. "
        "title, description, general_discussion[i], and "
        "code_review_threads[j].comments[k] belong only to that pull request.\n\n"
        f"{json.dumps(payload, indent=2)}"
    )
    return [
        Message(role="system", content=BATCH_SYSTEM_PROMPT),
        Message(role="user", content=content),
    ]
