"""Map–Reduce synthesis of the User Guideline from consolidated security discussions."""

import json
from typing import Any

from asgard.providers.models import Message

MAP_PROMPT = """\
Below are security discussions from pull requests of {repository}: for each,
its pr_id, title, evidence quotes and the classifier's rationale.

For each pull request, extract the concrete rules a contributor should follow,
as stated or fixed in that pull request.

- Each rule is about exactly one artifact: a setting, environment variable,
  function, class, module, template tag or filter, CLI flag, header, or
  dependency. If the pull request touches several artifacts or gives several
  pieces of advice, return one rule for each.
- artifact is the short name as written in the text, without module path,
  parentheses or decorator sign (e.g. constant_time_compare, ALLOWED_HOSTS).
- Use only that pull request's own text. If it states no concrete rule, return
  no rules for it. Never use one pull request to explain another.

Return JSON: {"rules": [{"pr_id", "artifact", "rule", "risk"}]}. rule is one
imperative sentence; risk is one sentence on what goes wrong otherwise.
"""

REDUCE_PROMPT = """\
Below are candidate rules extracted from security pull requests of
{repository}, each with pr_id, artifact, rule and risk. Write the User
Guidelines section of its SECURITY.md.

Work in this order:
1. Group the candidates by artifact.
2. For each artifact, write one recommendation per distinct piece of advice.
   Candidates with the same artifact and the same advice share a
   recommendation; anything else stays separate.
3. Only then group the finished recommendations into thematic topics. Topics
   never merge, shorten or drop recommendations.

Rules:
- A recommendation is about exactly one artifact, and its title names it.
  Never combine artifacts: rules about a password hasher, its salt and its
  iteration count are three recommendations, not one.
- There are at least as many recommendations as distinct artifacts.
- Cite exactly the pr_ids of the candidates the recommendation covers.
  Every candidate is covered by one recommendation.
- Title: a short imperative. Text: Detailed sentences with the concrete risk and the
  correct practice, using only what the candidates say.

Return JSON: {"topics": [{"title", "recommendations":
[{"title", "text", "pr_ids"}]}]}.
"""

# Forge / registry "synthesizer" role edits the reduce prompt.
SYSTEM_PROMPT = REDUCE_PROMPT

_MAP_FIELDS = ("pr_id", "title", "evidence", "rationale")


def fill_repository(prompt: str, repository: str) -> str:
    """Replace ``{repository}`` without interpreting other braces (JSON examples)."""
    return prompt.replace("{repository}", repository)


def map_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return the per-PR payload sent to the map step (no ``link``)."""
    return [{key: record[key] for key in _MAP_FIELDS} for record in records]


def build_map_messages(
    repository: str,
    records: list[dict[str, Any]],
    system_prompt: str | None = None,
) -> list[Message]:
    """Return the system prompt and one user message for a map batch.

    ``system_prompt`` overrides ``MAP_PROMPT`` when set. Substitution is literal
    so JSON braces in the prompt stay intact.
    """
    prompt = MAP_PROMPT if system_prompt is None else system_prompt
    return [
        Message(role="system", content=fill_repository(prompt, repository)),
        Message(role="user", content=json.dumps(map_records(records), indent=2)),
    ]


def build_reduce_messages(
    repository: str,
    rules: list[dict[str, Any]],
    system_prompt: str | None = None,
) -> list[Message]:
    """Return the system prompt and one user message for the reduce step.

    ``system_prompt`` overrides ``REDUCE_PROMPT`` when set. Substitution is
    literal so JSON braces in the prompt stay intact.
    """
    prompt = REDUCE_PROMPT if system_prompt is None else system_prompt
    return [
        Message(role="system", content=fill_repository(prompt, repository)),
        Message(role="user", content=json.dumps(rules, indent=2)),
    ]


def build_messages(
    repository: str,
    records: list[dict[str, Any]],
    system_prompt: str | None = None,
) -> list[Message]:
    """Backward-compatible alias for :func:`build_reduce_messages`.

    Historical callers passed consolidated records; prefer the map/reduce
    builders for new code.
    """
    return build_reduce_messages(repository, records, system_prompt=system_prompt)
