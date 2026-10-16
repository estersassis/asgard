"""Synthesize the User Guideline from consolidated security discussions."""

import json
from typing import Any

from asgard.providers.models import Message

SYSTEM_PROMPT = """\
Based on the security classifications extracted from the following {repository}
Pull Request discussions, generate the User Guideline section for the
{repository} repository's security policy (SECURITY.md).

Goal: produce fine-grained, repository-specific security recommendations that a
contributor can follow — naming concrete settings, APIs, classes, flags, or
misconfigurations (e.g. "Never set DEBUG = True in production", "Do not apply
|safe to user-supplied values"), not broad themes (e.g. "Session Integrity",
"Prevent SQL Injection", "Secure Configuration").

Hard coverage rule (do not ignore):
- If the input contains 40 or more security pull requests, you MUST return at
  least 30 recommendations in total across all topics. Returning 8–15 is a
  failure: those counts mean you over-merged. Keep expanding until you reach 30+.
- Target roughly 30–45 recommendations for large inputs; quality over padding,
  but never stop early by summarizing many PRs into one REC.
- Scan the full input. Cover distinct patterns (config, auth/sessions, crypto,
  SQL/ORM, XSS/templates, access control, CSRF, redirects/hosts, cookies/TLS,
  dependencies, logging/tracebacks, exception handling, admin, uploads, etc.)
  whenever the evidence supports them.

Granularity:
- One recommendation = one concrete control, misconfiguration, or fix pattern.
  Do not bundle unrelated issues (password hashing + session signing + TLS)
  into a single recommendation.
- Split when the actionable advice differs (different setting, API, dependency,
  or failure mode). Prefer citing 1–3 closely related PRs per recommendation.

Specificity (required):
- Every title and body must name project-specific artifacts from the evidence:
  settings, env vars, functions, classes, modules, template filters, CLI flags,
  dependency names/versions, or protocol/header names discussed in the PRs.
- Titles are short imperatives that state the rule (what to do / not do), not
  category labels.
- Bodies explain the concrete risk and the correct practice in 1–3 sentences,
  grounded in what the PR discussions actually said or fixed.
- Strictly exclude generic advice that is not traceable to the provided
  evidence (no textbook OWASP filler).

Cite pull requests only as numbers that appear as pr_id in the input.
Every recommendation cites at least one of those numbers, and no other number.

Organize into thematic topics only as grouping; recommendations inside must
still be specific. Before you finish, count the recommendations; if the input
has ≥40 PRs and your count is below 30, split further and add uncovered
controls from the remaining evidence.

Return JSON with exactly these fields:
- topics: a list of topics found in the data

Each topic has:
- title: the topic name
- recommendations: a list of recommendations

Each recommendation has:
- title: a short imperative title naming the concrete control
- text: the instruction, grounded in the evidence, naming project-specific
  artifacts
- pr_ids: a non-empty list of pr_id values from the input
"""


def build_messages(
    repository: str,
    records: list[dict[str, Any]],
    system_prompt: str | None = None,
) -> list[Message]:
    """Return the system prompt and one user message for this repository.

    ``repository`` is owner/name and replaces ``{repository}`` in the system prompt.
    ``system_prompt`` overrides ``SYSTEM_PROMPT`` when set (same ``{repository}`` template).
    The user message is the JSON of the full consolidated list.
    """
    prompt = SYSTEM_PROMPT if system_prompt is None else system_prompt
    return [
        Message(role="system", content=prompt.format(repository=repository)),
        Message(role="user", content=json.dumps(records, indent=2)),
    ]
