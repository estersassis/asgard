"""Turn consolidated security discussions into the User Guideline (map–reduce)."""

import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from asgard.core.config import ConfigError
from asgard.core.events import CitationWarning, SynthesisChunk
from asgard.core.prompt_registry import resolve_map_prompt, resolve_reduce_prompt
from asgard.core.stage import RunContext, Stage
from asgard.prompts.synthesizer import (
    assemble_topics,
    build_map_messages,
    build_reduce_messages,
    category_label,
    group_by_category,
)
from asgard.providers.factory import LLMFactory
from asgard.providers.models import Message

_INPUT = "synthesize stage requires consolidated pull requests"
_FIELDS = ("pr_id", "title", "link", "evidence", "rationale")
_EMPTY = "There was no security discussion, so there is no guideline."
_OUTSIDE = "synthesis cites a pull request outside the input"
_INVALID = "synthesis response is invalid"
_CITATION_WARN = "citation #{pr_id} is not in the approved security set"
_MAP_BATCH = 20
_REDUCE_BATCH = 50
_MAP_OUTPUT = "map.json"


class MapItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pr_id: str = Field(min_length=1)
    owasp: str = Field(min_length=1)
    title: str = Field(min_length=1)
    text: str = Field(min_length=1)

    @field_validator("pr_id", mode="before")
    @classmethod
    def _pr_id(cls, value: Any) -> Any:
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return str(value)
        return value


class MapBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[MapItem] = Field(default_factory=list)


class Recommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    text: str = Field(min_length=1)
    pr_ids: list[str] = Field(min_length=1)

    @field_validator("pr_ids", mode="before")
    @classmethod
    def _numbers(cls, value: Any) -> Any:
        if not isinstance(value, list):
            return value
        numbers: list[Any] = []
        for item in value:
            if isinstance(item, bool):
                return value
            if isinstance(item, int):
                numbers.append(str(item))
            else:
                numbers.append(item)
        return numbers

    @field_validator("pr_ids")
    @classmethod
    def _present(cls, value: list[str]) -> list[str]:
        if any(not item for item in value):
            raise ValueError("pr_id must be non-empty")
        return value


class Topic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    recommendations: list[Recommendation] = Field(min_length=1)


class Synthesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topics: list[Topic] = Field(min_length=1)


class Recommendations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommendations: list[Recommendation] = Field(min_length=1)


class SynthesizeStage(Stage):
    name = "synthesize"

    def __init__(self, repo: str, factory: LLMFactory) -> None:
        self.repo = repo
        self.factory = factory

    def execute(self, ctx: RunContext, data: Any) -> str:
        records = _records(data)
        if not records:
            return _EMPTY
        allowed = {record["pr_id"] for record in records}
        config = _run_config(ctx)
        try:
            map_prompt = resolve_map_prompt(config, ctx.run_dir)
            reduce_prompt = resolve_reduce_prompt(config, ctx.run_dir)
        except LookupError as exc:
            raise ConfigError(str(exc)) from None

        items = _load_map(ctx)
        if items is None:
            items = _map(self.factory, ctx, self.repo, records, map_prompt)
            _write_map(ctx, items)
        if not items:
            return _EMPTY

        sections = _reduce(
            self.factory,
            ctx,
            self.repo,
            items,
            reduce_prompt,
            allowed,
        )
        if not any(sections.values()):
            return _EMPTY
        return _markdown(self.repo, Synthesis.model_validate(assemble_topics(sections)))


def _map(
    factory: LLMFactory,
    ctx: RunContext,
    repo: str,
    records: list[dict[str, Any]],
    map_prompt: str,
) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for start in range(0, len(records), _MAP_BATCH):
        batch = records[start : start + _MAP_BATCH]
        batch_ids = {record["pr_id"] for record in batch}
        messages = build_map_messages(repo, batch, system_prompt=map_prompt)
        response = factory.generate("synthesizer", messages, schema=MapBatch)
        ctx.emit(SynthesisChunk(text=response.text))
        parsed = response.parsed
        if not isinstance(parsed, MapBatch):
            raise ConfigError(_INVALID)
        for item in parsed.items:
            if item.pr_id not in batch_ids:
                continue
            items.append(item.model_dump())
    return items


def _write_map(ctx: RunContext, items: list[dict[str, str]]) -> None:
    path = ctx.run_dir / "synthesize" / _MAP_OUTPUT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, indent=2) + "\n", encoding="utf-8")


def _load_map(ctx: RunContext) -> list[dict[str, str]] | None:
    """Reuse map.json from a prior attempt (e.g. reduce failed after map finished)."""
    path = ctx.run_dir / "synthesize" / _MAP_OUTPUT
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, list) or not data:
        return None
    items: list[dict[str, str]] = []
    for item in data:
        if not isinstance(item, dict):
            return None
        pr_id = item.get("pr_id")
        owasp = item.get("owasp")
        title = item.get("title")
        text = item.get("text")
        if not all(isinstance(value, str) and value for value in (pr_id, owasp, title, text)):
            return None
        items.append({"pr_id": pr_id, "owasp": owasp, "title": title, "text": text})
    return items


def _reduce(
    factory: LLMFactory,
    ctx: RunContext,
    repo: str,
    items: list[dict[str, str]],
    reduce_prompt: str,
    allowed: set[str],
) -> dict[str, list[dict[str, Any]]]:
    """Reduce each OWASP category, in batches, so one reply cannot truncate."""
    sections: dict[str, list[dict[str, Any]]] = {}
    for category, group in group_by_category(items).items():
        parts: list[list[Recommendation]] = []
        last_messages: list[Message] | None = None
        last_text = ""
        for start in range(0, len(group), _REDUCE_BATCH):
            batch = group[start : start + _REDUCE_BATCH]
            messages = build_reduce_messages(
                repo, batch, category, system_prompt=reduce_prompt
            )
            response = factory.generate("synthesizer", messages, schema=Recommendations)
            ctx.emit(SynthesisChunk(text=response.text))
            parts.append(_recommendations(response.parsed).recommendations)
            last_messages = messages
            last_text = response.text
        if last_messages is None:
            raise ConfigError(_INVALID)
        if len(parts) == 1:
            kept = _repair(factory, ctx, last_messages, last_text, parts[0], allowed)
        else:
            kept = _strip_batch(ctx, [item for part in parts for item in part], allowed)
        if kept:
            sections[category] = [item.model_dump() for item in kept]
    return sections


def _strip_batch(
    ctx: RunContext, recommendations: list[Recommendation], allowed: set[str]
) -> list[Recommendation]:
    guideline = _wrap(recommendations)
    if guideline is None or not _outside(guideline, allowed):
        return recommendations
    cleaned = _accept_citations(ctx, guideline, allowed)
    return cleaned.topics[0].recommendations


def _wrap(recommendations: list[Recommendation]) -> Synthesis | None:
    if not recommendations:
        return None
    return Synthesis(
        topics=[Topic(title=category_label("A01"), recommendations=recommendations)]
    )


def _run_config(ctx: RunContext) -> dict[str, Any] | None:
    path = ctx.run_dir / "config.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ConfigError("run configuration is invalid") from None
    if not isinstance(data, dict):
        raise ConfigError("run configuration is invalid")
    return data


def _records(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, list):
        raise ConfigError(_INPUT)
    seen: set[str] = set()
    records: list[dict[str, Any]] = []
    for raw in data:
        record = _record(raw)
        pr_id = record["pr_id"]
        if pr_id in seen:
            raise ConfigError(f"duplicate pull request {pr_id}")
        seen.add(pr_id)
        records.append(record)
    return records


def _record(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or set(raw) != set(_FIELDS):
        raise ConfigError(_INPUT)
    pr_id = raw.get("pr_id")
    title = raw.get("title")
    link = raw.get("link")
    evidence = raw.get("evidence")
    rationale = raw.get("rationale")
    if not isinstance(pr_id, str) or not pr_id:
        raise ConfigError(_INPUT)
    if not isinstance(title, str) or not isinstance(link, str) or not isinstance(rationale, str):
        raise ConfigError(_INPUT)
    if not isinstance(evidence, list) or any(not _quote(item) for item in evidence):
        raise ConfigError(_INPUT)
    return raw


def _quote(item: Any) -> bool:
    if not isinstance(item, dict) or set(item) != {"quote", "location"}:
        return False
    return isinstance(item.get("quote"), str) and isinstance(item.get("location"), str)


def _recommendations(parsed: Any) -> Recommendations:
    if not isinstance(parsed, Recommendations):
        raise ConfigError(_INVALID)
    return parsed


def _outside(guideline: Synthesis, allowed: set[str]) -> set[str]:
    cited = {
        pr_id
        for topic in guideline.topics
        for recommendation in topic.recommendations
        for pr_id in recommendation.pr_ids
    }
    return cited - allowed


def _strip_outside(
    guideline: Synthesis, allowed: set[str]
) -> tuple[Synthesis | None, list[str]]:
    """Drop unknown pr_ids. Keep recommendations that still have at least one valid id."""
    dropped: list[str] = []
    topics: list[Topic] = []
    for topic in guideline.topics:
        recommendations: list[Recommendation] = []
        for recommendation in topic.recommendations:
            kept = [pr_id for pr_id in recommendation.pr_ids if pr_id in allowed]
            lost = [pr_id for pr_id in recommendation.pr_ids if pr_id not in allowed]
            dropped.extend(lost)
            if not kept:
                continue
            recommendations.append(
                Recommendation(
                    title=recommendation.title,
                    text=recommendation.text,
                    pr_ids=kept,
                )
            )
        if recommendations:
            topics.append(Topic(title=topic.title, recommendations=recommendations))
    cleaned = Synthesis(topics=topics) if topics else None
    return cleaned, sorted(set(dropped), key=_order)


def _accept_citations(
    ctx: RunContext,
    guideline: Synthesis,
    allowed: set[str],
) -> Synthesis:
    """After a failed model repair, strip unknown ids instead of failing the run."""
    cleaned, dropped = _strip_outside(guideline, allowed)
    if cleaned is None:
        raise ConfigError(_OUTSIDE)
    for pr_id in dropped:
        ctx.emit(
            CitationWarning(
                pr_id=pr_id,
                message=_CITATION_WARN.format(pr_id=pr_id),
            )
        )
    return cleaned


def _repair(
    factory: LLMFactory,
    ctx: RunContext,
    messages: list[Message],
    text: str,
    recommendations: list[Recommendation],
    allowed: set[str],
) -> list[Recommendation]:
    """Ask the model once for citation fixes; strip leftovers if repair fails."""
    guideline = _wrap(recommendations)
    if guideline is None or not _outside(guideline, allowed):
        return recommendations
    text, repaired = _retry(
        factory, ctx, messages, _citation_note(text, guideline, allowed)
    )
    _ = text
    guideline = _wrap(repaired)
    if guideline is not None and _outside(guideline, allowed):
        guideline = _accept_citations(ctx, guideline, allowed)
    if guideline is None:
        raise ConfigError(_OUTSIDE)
    return guideline.topics[0].recommendations


def _retry(
    factory: LLMFactory,
    ctx: RunContext,
    messages: list[Message],
    note: str,
) -> tuple[str, list[Recommendation]]:
    try:
        response = factory.generate(
            "synthesizer",
            [*messages, Message(role="user", content=note)],
            schema=Recommendations,
        )
    except (ValidationError, ValueError):
        raise ConfigError(_INVALID) from None
    ctx.emit(SynthesisChunk(text=response.text))
    return response.text, _recommendations(response.parsed).recommendations


def _citation_note(text: str, guideline: Synthesis, allowed: set[str]) -> str:
    known = ", ".join("#" + pr_id for pr_id in sorted(allowed, key=_order))
    rejected = ", ".join("#" + pr_id for pr_id in sorted(_outside(guideline, allowed), key=_order))
    return (
        "Cite only pull requests from the input. "
        f"Allowed ids: {known}. "
        f"These ids are not in the input: {rejected}. "
        "Reply with JSON only, matching the requested schema.\n\n"
        f"{text}"
    )


def _order(pr_id: str) -> tuple[int, int | str]:
    if pr_id.isdecimal():
        return (0, int(pr_id))
    return (1, pr_id)


def _markdown(repo: str, guideline: Synthesis) -> str:
    name = repo.split("/", 1)[-1]
    lines = [f"# {name} Security Policy — User Guidelines", ""]
    number = 1
    blocks: list[str] = []
    for index, topic in enumerate(guideline.topics, 1):
        recommendations: list[str] = []
        for recommendation in topic.recommendations:
            cites = ", ".join(f"#{pr_id}" for pr_id in recommendation.pr_ids)
            recommendations.append(
                f"**REC-{number:02d} — {recommendation.title}.**\n"
                f"{recommendation.text} ({cites})"
            )
            number += 1
        blocks.append(f"## {index}. {topic.title}\n\n" + "\n\n".join(recommendations))
    lines.append("\n\n".join(blocks))
    return "\n".join(lines) + "\n"
