"""Binary prompt: does this pull request contain a security discussion?"""

import json
from collections.abc import Sequence

from asgard.core.normalization import NormalizedPullRequest
from asgard.providers.models import Message

SYSTEM_PROMPT = """\
For each pull request, decide whether its own text contains a security discussion.

Answer true only when the text states a security risk or a protection against one:
a vulnerability or attack, an access control or authentication flaw, exposed secrets,
unsafe input handling, an insecure default, or a dependency update made for a
security reason.

Answer false when security-related words or components appear without a stated
risk. Words like secure, token, hash, session, permission, sanitize or auth are not
evidence by themselves. Test: does the text say what could go wrong for security,
or what is being protected? If not, answer false.

Judge each pull request independently, using only its title, description,
general_discussion and code_review_threads. Do not infer a risk the text does not state.

Evidence: each quote is a non-empty literal excerpt from the cited location in the
same pull request, and must be the sentence that states the risk, not a lone keyword.
Locations (zero-based, per pull request): title, description, general_discussion[i],
code_review_threads[j].comments[k].

Return JSON: {"results": [{"pr_id", "has_security_discussion", "evidence":
[{"quote", "location"}], "rationale"}]}. If true, evidence has at least one item;
if false, evidence is []. The rationale is one sentence naming the risk, or saying
why there is none.

Revision: if an item includes a previous answer and evaluator feedback, revise only
that pull request. The feedback is not pull request text. Change the decision only
if the feedback says it is wrong; otherwise fix only the evidence with a new literal
quote or a corrected location, never the rejected evidence unchanged.
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
For each pull request in the list, decide whether its own text contains a
security discussion. Judge each one independently; never use text from one
pull request as evidence for another.

Answer true only when the text states a security risk or a protection against
one: a vulnerability or attack, an access control or authentication flaw,
exposed secrets, unsafe input handling, an insecure default, or a dependency
update made for a security reason.

Answer false when security-related words or components appear without a stated
risk. Words like secure, token, hash, session, permission, sanitize or auth are
not evidence by themselves. Test: does the text say what could go wrong for
security, or what is being protected? If not, answer false. Do not infer a risk
the text does not state.

Read the title, description, general_discussion and code_review_threads.
Evidence: each quote is a non-empty literal excerpt from the cited location in
the same pull request, and must be the sentence that states the risk, not a lone
keyword. Locations (zero-based, restarting for each pull request): title,
description, general_discussion[i], code_review_threads[j].comments[k].

Return JSON: {"results": [{"pr_id", "has_security_discussion", "evidence":
[{"quote", "location"}], "rationale"}]}, one object per pull request. If true,
evidence has at least one item; if false, evidence is []. The rationale is one
sentence naming the risk, or saying why there is none.

Revision: an item may include your previous answer and evaluator feedback.
Revise only that pull request; the feedback is not pull request text and does
not apply to other items. Change the decision only if the feedback starts with
"The decision is wrong:"; otherwise fix only the evidence with a new literal
quote or a corrected location, never the rejected evidence unchanged.
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
