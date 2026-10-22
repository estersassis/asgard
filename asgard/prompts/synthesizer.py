"""Synthesis of the SECURITY.md contributor guidelines, organized by OWASP Top 10:2025.

1. Map: pull requests become grouped recommendations. The whole set goes in
   one call, or in batches when a batch size is set.
2. Reduce: every map recommendation goes into one call.
3. Group (code): the reduced recommendations are normalized, deduplicated by
   title and ordered by category, with a final "Security Process" section for
   project practices that fit no OWASP category.
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

# Extra section for project practices outside the OWASP Top 10. It is also the
# fallback for an invalid category id returned by the LLM.
OTHER = "OTHER"
OTHER_LABEL = "Security Process"
OTHER_DESCRIPTION = (
    "vulnerability reporting, disclosure, security releases, release notes "
    "and backports; use only when no category above fits"
)

MAP_PROMPT = """\
Below are security pull requests of {repository}, each with pr_id, date,
evidence and rationale.

Write recommendations for contributors of this codebase. Each one covers one
component or risk and groups the pull requests about it:
- title: imperative, max 10 words.
- risk: one sentence.
- actions: short bullets on what to do when changing that code.
- pr_ids: the grouped pull requests.
Use only the input.

Assign each recommendation the category that best fits it:
{owasp_categories}

Return JSON: {"items": [{"owasp", "title", "risk", "actions", "pr_ids"}]},
where owasp is a category id such as A05 or OTHER.
"""

REDUCE_PROMPT = """\
Below are recommendations for contributors of {repository}, each with id,
owasp, date, title, risk and actions. They were written separately, so
several may cover the same component or risk.

Merge recommendations about the same component or risk into one, even if
they are in different categories, and put it in the category that fits
best:
{owasp_categories}

Rewrite merged ones in the same format: title (max 10 words), risk (one
sentence), up to 3 actions. If advice conflicts, follow the most recent
date.

Return at most {max_recs} recommendations per category, leaving out the
least important. Use only the input.

Return JSON: {"recommendations": [{"owasp", "title", "risk", "actions",
"sources"}]}, where owasp is a category id such as A05 or OTHER, and sources
lists the ids of the input recommendations merged into it.
"""

# Forge / registry "synthesizer" role edits the reduce prompt.
SYSTEM_PROMPT = REDUCE_PROMPT

CATEGORY_ORDER: list[str] = [*OWASP_TOP10_2025, OTHER]


def fill(prompt: str, **values: str) -> str:
    """Replace ``{name}`` literally so JSON braces in the prompt stay intact."""
    for name, value in values.items():
        prompt = prompt.replace("{" + name + "}", value)
    return prompt


def categories_text() -> str:
    """Category list given to both prompts, ending with the OTHER section."""
    lines = [f"- {cid} {name}" for cid, name in OWASP_TOP10_2025.items()]
    lines.append(f"- {OTHER} {OTHER_LABEL}: {OTHER_DESCRIPTION}")
    return "\n".join(lines)


def category_label(cid: str) -> str:
    """Section heading, e.g. 'A05:2025 Injection' or 'Security Process'."""
    if cid in OWASP_TOP10_2025:
        return f"{cid}:2025 {OWASP_TOP10_2025[cid]}"
    return OTHER_LABEL


def normalize_category(value: Any) -> str:
    """Map an LLM category value to a known id; anything unknown becomes OTHER."""
    cid = str(value or "").strip().upper()
    if cid.startswith(OTHER):
        return OTHER
    cid = cid[:3]
    return cid if cid in OWASP_TOP10_2025 else OTHER


def _with_date(row: dict[str, Any], date: Any, after: str) -> dict[str, Any]:
    """Insert ``date`` right after the ``after`` key when it is a non-empty string."""
    if not (isinstance(date, str) and date):
        return row
    out: dict[str, Any] = {}
    for key, value in row.items():
        out[key] = value
        if key == after:
            out["date"] = date
    return out


def _map_row(record: dict[str, Any]) -> dict[str, Any]:
    row = {
        "pr_id": record["pr_id"],
        "evidence": record["evidence"],
        "rationale": record["rationale"],
    }
    return _with_date(row, record.get("date"), after="pr_id")


def build_map_messages(
    repository: str, records: list[dict[str, Any]], system_prompt: str | None = None
) -> list[Message]:
    """Messages for one map batch (``link`` is not sent)."""
    payload = [_map_row(record) for record in records]
    prompt = fill(
        system_prompt or MAP_PROMPT,
        repository=repository,
        owasp_categories=categories_text(),
    )
    return [
        Message(role="system", content=prompt),
        Message(role="user", content=json.dumps(payload, indent=2)),
    ]


def group_by_category(items: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    """Bucket items by category id, in Top 10 order with OTHER last; empty ones omitted."""
    groups: dict[str, list[dict[str, Any]]] = {cid: [] for cid in CATEGORY_ORDER}
    for item in items:
        item["owasp"] = normalize_category(item.get("owasp"))
        groups[item["owasp"]].append(item)
    return {cid: group for cid, group in groups.items() if group}


def per_category_cap(count: int) -> int:
    """Per-category limit given to the reduce prompt: 30% of the pull requests, at least 1.

    ``count`` is the number of pull requests in the synthesis input. The same
    ceiling is applied to every category, so it is a loose cap rather than a
    share of that category.
    """
    if count <= 0:
        return 0
    return max(1, (count * 30) // 100)


# Kept for callers that still use the old name.
recommendation_cap = per_category_cap


def _reduce_row(item: dict[str, Any]) -> dict[str, Any]:
    row = {
        "id": item["id"],
        "owasp": item["owasp"],
        "title": item["title"],
        "risk": item["risk"],
        "actions": item["actions"],
    }
    return _with_date(row, item.get("date"), after="owasp")


def build_reduce_messages(
    repository: str,
    items: list[dict[str, Any]],
    system_prompt: str | None = None,
    max_recs: int | None = None,
) -> list[Message]:
    """One reduce call. The model may move items between categories."""
    if max_recs is None:
        max_recs = per_category_cap(len(items))
    payload = [_reduce_row(item) for item in items]
    prompt = fill(
        system_prompt or REDUCE_PROMPT,
        repository=repository,
        owasp_categories=categories_text(),
        max_recs=str(max_recs),
    )
    return [
        Message(role="system", content=prompt),
        Message(role="user", content=json.dumps(payload, indent=2)),
    ]


def _title_key(title: Any) -> str:
    return " ".join(str(title or "").lower().split()).rstrip(".")


def merge_same_title(recommendations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge recommendations with the same category and title, joining their sources.

    Safety net for the model repeating one recommendation with its sources
    split across copies. The first copy keeps its text; sources are joined
    in order without duplicates.
    """
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for rec in recommendations:
        rec["owasp"] = normalize_category(rec.get("owasp"))
        key = (rec["owasp"], _title_key(rec.get("title")))
        if key not in merged:
            rec["sources"] = list(dict.fromkeys(rec.get("sources") or []))
            merged[key] = rec
            continue
        target = merged[key]
        target["sources"] = list(
            dict.fromkeys([*target["sources"], *(rec.get("sources") or [])])
        )
    return list(merged.values())


def assemble_topics(sections: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """Final structure in Top 10 order, Security Process last:
    ``{"topics": [{"title", "recommendations"}]}``."""
    return {
        "topics": [
            {"title": category_label(cid), "recommendations": sections[cid]}
            for cid in CATEGORY_ORDER
            if sections.get(cid)
        ]
    }