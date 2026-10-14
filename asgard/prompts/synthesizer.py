"""Synthesize the User Guideline from consolidated security discussions."""

import json
from typing import Any

from asgard.providers.models import Message

SYSTEM_PROMPT = """\
Based on the security classifications extracted from the following {repository}
Pull Request discussions, generate the User Guideline section for the
{repository} repository's security policy (SECURITY.md).

The guideline must provide security-related and project-specific instructions,
including the secure usage of dependencies, recommended configurations, and
general project usage based on the security patterns and lessons learned
identified in the provided data.

The final guideline must strictly avoid generic recommendations, focusing only
on the provided evidence.

Cite pull requests only as numbers that appear as pr_id in the input.
Every recommendation cites at least one of those numbers, and no other number.

Return JSON with exactly these fields:
- topics: a list of topics found in the data

Each topic has:
- title: the topic name
- recommendations: a list of recommendations

Each recommendation has:
- title: a short title
- text: the instruction, grounded in the evidence
- pr_ids: a non-empty list of pr_id values from the input
"""


def build_messages(repository: str, records: list[dict[str, Any]]) -> list[Message]:
    """Return the system prompt and one user message for this repository.

    ``repository`` is owner/name and replaces ``{repository}`` in the system prompt.
    The user message is the JSON of the full consolidated list.
    """
    return [
        Message(role="system", content=SYSTEM_PROMPT.format(repository=repository)),
        Message(role="user", content=json.dumps(records, indent=2)),
    ]
