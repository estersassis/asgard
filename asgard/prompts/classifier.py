"""Binary prompt: does this pull request contain a security discussion?"""

import json
from collections.abc import Sequence

from asgard.core.normalization import NormalizedPullRequest
from asgard.providers.models import Message

SYSTEM_PROMPT = """\
Decide whether one pull request's own text contains a security discussion.

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
Evidence: each quote is a non-empty literal excerpt from the cited location, and
must be the sentence that states the risk, not a lone keyword. Locations
(zero-based): title, description, general_discussion[i],
code_review_threads[j].comments[k].

Return JSON: {"has_security_discussion", "evidence": [{"quote", "location"}],
"rationale"}. If true, evidence has at least one item; if false, evidence is [].
The rationale is one sentence naming the risk, or saying why there is none.

Revision: your previous answer and evaluator feedback may follow the pull
request. The feedback is not pull request text. Change the decision only if the
feedback starts with "The decision is wrong:"; otherwise fix only the evidence
with a new literal quote or a corrected location, never the rejected evidence
unchanged.
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


def resolve_batch_system_prompt(system_prompt: str | None = None) -> str:
    """Map a solo/registry prompt onto a batch-capable system prompt.

    The built-in solo prompt becomes :data:`BATCH_SYSTEM_PROMPT`. Custom solo
    prompts keep their criteria and gain the batch JSON envelope. Prompts that
    already describe a ``results`` list are used as-is.
    """
    if system_prompt is None or system_prompt.strip() == SYSTEM_PROMPT.strip():
        return BATCH_SYSTEM_PROMPT
    if '"results"' in system_prompt or "results\":" in system_prompt:
        return system_prompt
    return (
        "You are classifying several pull requests in one call. "
        "Judge each independently; never use text from one as evidence for another.\n\n"
        f"{system_prompt.strip()}\n\n"
        "For this call, ignore any single-PR-only return format above and "
        'return JSON: {"results": [{"pr_id", "has_security_discussion", '
        '"evidence": [{"quote", "location"}], "rationale"}]}, one object per '
        "pull request. If true, evidence has at least one item; if false, "
        "evidence is []."
    )


def build_batch_messages(
    items: Sequence[tuple[NormalizedPullRequest, str | None, str | None]],
    *,
    system_prompt: str | None = None,
) -> list[Message]:
    """Return the batch system prompt and one user message for these pull requests.

    Each item is a pull request plus, on a later attempt, that pull request's
    previous response and evaluator feedback. ``previous`` and ``feedback``
    are included together or omitted together. Feedback applies only to the
    pull request it is attached to.
    ``system_prompt`` overrides the built-in batch prompt when set (solo
    prompts are adapted via :func:`resolve_batch_system_prompt`).
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
        Message(role="system", content=resolve_batch_system_prompt(system_prompt)),
        Message(role="user", content=content),
    ]
