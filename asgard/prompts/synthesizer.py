"""Synthesis of the SECURITY.md User Guidelines, organized by OWASP Top 10:2025.

1. Map: each PR's rationale is rewritten as one recommendation and tagged
   with an OWASP category.
2. Group (code): recommendations are bucketed by category, in Top 10 order.
3. Reduce: per category, duplicate recommendations are merged and PRs cited.
"""

import json
from typing import Any

from asgard.providers.models import Message

OWASP_TOP10_2025: dict[str, str] = {
    "A01": "Broken Access Control",
    "A02": "Security Misconfiguration",
    "A03": "Software Supply Chain Failures",
    "A04": "Cryptographic Failures",
    "A05": "Injection",
    "A06": "Insecure Design",
    "A07": "Authentication Failures",
    "A08": "Software or Data Integrity Failures",
    "A09": "Security Logging & Alerting Failures",
    "A10": "Mishandling of Exceptional Conditions",
}
OTHER = "OTHER"  # fallback for an invalid category id returned by the LLM

MAP_PROMPT = """\
Below are security pull requests of {repository}, each with pr_id, title,
evidence and rationale.

For each pull request, rewrite its rationale as one recommendation for
contributors: a short imperative title and one or two sentences with the risk
and the correct practice. Use only that pull request's text.

Then assign the OWASP Top 10:2025 category that best fits it:
{owasp_categories}

Return JSON: {"items": [{"pr_id", "owasp", "title", "text"}]}, where owasp is
a category id such as A05.
"""

REDUCE_PROMPT = """\
Below are recommendations from security pull requests of {repository}, all in
the section "{category}" of its SECURITY.md.

Merge recommendations that give the same advice into one, citing all of their
pr_ids. Keep every other recommendation as is. Do not drop any pr_id and do
not add advice that is not in the input.

Return JSON: {"recommendations": [{"title", "text", "pr_ids"}]}.
"""

# Forge / registry "synthesizer" role edits the reduce prompt.
SYSTEM_PROMPT = REDUCE_PROMPT


def fill(prompt: str, **values: str) -> str:
    """Replace ``{name}`` literally so JSON braces in the prompt stay intact."""
    for name, value in values.items():
        prompt = prompt.replace("{" + name + "}", value)
    return prompt


def category_label(cid: str) -> str:
    """Section heading, e.g. 'A05:2025 Injection'."""
    return f"{cid}:2025 {OWASP_TOP10_2025[cid]}" if cid in OWASP_TOP10_2025 else "Other"


def build_map_messages(
    repository: str, records: list[dict[str, Any]], system_prompt: str | None = None
) -> list[Message]:
    """Messages for one map batch (``link`` is not sent)."""
    categories = "\n".join(f"- {cid} {name}" for cid, name in OWASP_TOP10_2025.items())
    payload = [
        {k: r[k] for k in ("pr_id", "title", "evidence", "rationale")} for r in records
    ]
    prompt = fill(
        system_prompt or MAP_PROMPT, repository=repository, owasp_categories=categories
    )
    return [
        Message(role="system", content=prompt),
        Message(role="user", content=json.dumps(payload, indent=2)),
    ]


def group_by_category(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Bucket map items by category id, in Top 10 order; empty ones omitted."""
    groups: dict[str, list[dict[str, Any]]] = {cid: [] for cid in [*OWASP_TOP10_2025, OTHER]}
    for item in items:
        cid = str(item.get("owasp", "")).strip().upper()[:3]
        item["owasp"] = cid if cid in OWASP_TOP10_2025 else OTHER
        groups[item["owasp"]].append(item)
    return {cid: group for cid, group in groups.items() if group}


def build_reduce_messages(
    repository: str,
    items: list[dict[str, Any]],
    category: str,
    system_prompt: str | None = None,
) -> list[Message]:
    """Messages for one category's reduce call."""
    payload = [{k: i[k] for k in ("pr_id", "title", "text")} for i in items]
    prompt = fill(
        system_prompt or REDUCE_PROMPT,
        repository=repository,
        category=category_label(category),
    )
    return [
        Message(role="system", content=prompt),
        Message(role="user", content=json.dumps(payload, indent=2)),
    ]


def assemble_topics(sections: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Final structure in Top 10 order: ``{"topics": [{"title", "recommendations"}]}``."""
    return {
        "topics": [
            {"title": category_label(cid), "recommendations": recs}
            for cid, recs in sections.items()
            if recs
        ]
    }