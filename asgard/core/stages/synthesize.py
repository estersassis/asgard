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
    group_by_category,
    merge_same_title,
    recommendation_cap,
)
from asgard.providers.factory import LLMFactory
from asgard.providers.models import Message

_INPUT = "synthesize stage requires consolidated pull requests"
_FIELDS = ("pr_id", "title", "link", "evidence", "rationale")
_EMPTY = "There was no security discussion, so there is no guideline."
_OUTSIDE = "synthesis cites a pull request outside the input"
_INVALID = "synthesis response is invalid"
_CITATION_WARN = "citation #{pr_id} is not in the approved security set"
_MAP_OUTPUT = "map.json"
_BATCH = "synthesize batch size must be >= 1"
_MAX_CITED = 15
_COVERAGE = 0.8


def _id_list(value: Any) -> Any:
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


def _actions(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    texts = [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return texts[:3]


class MapItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owasp: str = Field(min_length=1)
    title: str = Field(min_length=1)
    risk: str = Field(min_length=1)
    actions: list[str] = Field(min_length=1)
    pr_ids: list[str] = Field(min_length=1)

    @field_validator("pr_ids", mode="before")
    @classmethod
    def _pr_ids(cls, value: Any) -> Any:
        return _id_list(value)

    @field_validator("actions", mode="before")
    @classmethod
    def _actions(cls, value: Any) -> Any:
        return _actions(value)

    @field_validator("pr_ids")
    @classmethod
    def _present(cls, value: list[str]) -> list[str]:
        if any(not isinstance(item, str) or not item for item in value):
            raise ValueError("pr_id must be non-empty")
        return value


class MapBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[MapItem] = Field(default_factory=list)


class ReducedItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owasp: str = Field(min_length=1)
    title: str = Field(min_length=1)
    risk: str = Field(min_length=1)
    actions: list[str] = Field(min_length=1)
    sources: list[str] = Field(min_length=1)

    @field_validator("sources", mode="before")
    @classmethod
    def _sources(cls, value: Any) -> Any:
        return _id_list(value)

    @field_validator("actions", mode="before")
    @classmethod
    def _actions(cls, value: Any) -> Any:
        return _actions(value)

    @field_validator("sources")
    @classmethod
    def _present(cls, value: list[str]) -> list[str]:
        if any(not isinstance(item, str) or not item for item in value):
            raise ValueError("source id must be non-empty")
        return value


class Recommendations(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommendations: list[ReducedItem] = Field(default_factory=list)


class Recommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    risk: str = Field(min_length=1)
    actions: list[str] = Field(min_length=1)
    pr_ids: list[str] = Field(min_length=1)


class Topic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1)
    recommendations: list[Recommendation] = Field(min_length=1)


class Synthesis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topics: list[Topic] = Field(min_length=1)


class SynthesizeStage(Stage):
    name = "synthesize"

    def __init__(
        self, repo: str, factory: LLMFactory, map_batch: int | None = None
    ) -> None:
        if map_batch is not None and map_batch < 1:
            raise ConfigError(_BATCH)
        self.repo = repo
        self.factory = factory
        self.map_batch = map_batch

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

        _attach_dates(ctx, records)
        items = _load_map(ctx)
        if items is None:
            items = _split_cited(
                _map(self.factory, ctx, self.repo, records, map_prompt, self.map_batch)
            )
            _write_map(ctx, items)
        else:
            items = _split_cited(items)
        sections = (
            _reduce(
                self.factory,
                ctx,
                self.repo,
                _stamp(items, records),
                reduce_prompt,
                allowed,
                len(records),
            )
            if items
            else {}
        )
        sections = _cover(
            self.factory, ctx, self.repo, records, map_prompt, sections
        )
        if not any(sections.values()):
            return _EMPTY
        return _markdown(self.repo, Synthesis.model_validate(assemble_topics(sections)))


def _split_cited(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """No recommendation keeps more than 15 pull requests."""
    split: list[dict[str, Any]] = []
    for item in items:
        pr_ids = list(dict.fromkeys(item["pr_ids"]))
        if len(pr_ids) <= _MAX_CITED:
            row = dict(item)
            row["pr_ids"] = pr_ids
            split.append(row)
            continue
        for start in range(0, len(pr_ids), _MAX_CITED):
            row = dict(item)
            row["pr_ids"] = pr_ids[start : start + _MAX_CITED]
            split.append(row)
    return split


def _map(
    factory: LLMFactory,
    ctx: RunContext,
    repo: str,
    records: list[dict[str, Any]],
    map_prompt: str,
    map_batch: int | None,
    note: str | None = None,
) -> list[dict[str, Any]]:
    size = len(records) if map_batch is None else map_batch
    items: list[dict[str, Any]] = []
    for start in range(0, len(records), size):
        batch = records[start : start + size]
        batch_ids = {record["pr_id"] for record in batch}
        messages = build_map_messages(repo, batch, system_prompt=map_prompt)
        if note:
            messages = [
                messages[0],
                Message(role="user", content=f"{note}\n\n{messages[1].content}"),
            ]
        response = factory.generate("synthesizer", messages, schema=MapBatch)
        ctx.emit(SynthesisChunk(text=response.text))
        parsed = response.parsed
        if not isinstance(parsed, MapBatch):
            raise ConfigError(_INVALID)
        for item in parsed.items:
            pr_ids = [pr_id for pr_id in item.pr_ids if pr_id in batch_ids]
            if not pr_ids:
                continue
            dumped = item.model_dump()
            dumped["pr_ids"] = pr_ids
            items.append(dumped)
    return items


def _stamp(items: list[dict[str, Any]], records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Give each map recommendation an id and the latest date among its pull requests."""
    dates = {
        record["pr_id"]: record["date"]
        for record in records
        if isinstance(record.get("date"), str) and record["date"]
    }
    stamped: list[dict[str, Any]] = []
    for index, item in enumerate(items, 1):
        row = dict(item)
        row["id"] = str(index)
        cited = [dates[pr_id] for pr_id in item["pr_ids"] if pr_id in dates]
        if cited:
            row["date"] = max(cited)
        stamped.append(row)
    return stamped


def _write_map(ctx: RunContext, items: list[dict[str, Any]]) -> None:
    path = ctx.run_dir / "synthesize" / _MAP_OUTPUT
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, indent=2) + "\n", encoding="utf-8")


def _load_map(ctx: RunContext) -> list[dict[str, Any]] | None:
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
    items: list[dict[str, Any]] = []
    for item in data:
        if not _stored_map_item(item):
            return None
        items.append(
            {
                "owasp": item["owasp"],
                "title": item["title"],
                "risk": item["risk"],
                "actions": item["actions"],
                "pr_ids": item["pr_ids"],
            }
        )
    return items


def _stored_map_item(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    strings = (item.get("owasp"), item.get("title"), item.get("risk"))
    if not all(isinstance(value, str) and value for value in strings):
        return False
    actions = item.get("actions")
    pr_ids = item.get("pr_ids")
    if not isinstance(actions, list) or not actions:
        return False
    if not isinstance(pr_ids, list) or not pr_ids:
        return False
    if any(not isinstance(action, str) or not action for action in actions):
        return False
    return all(isinstance(pr_id, str) and pr_id for pr_id in pr_ids)


def _reduce(
    factory: LLMFactory,
    ctx: RunContext,
    repo: str,
    items: list[dict[str, Any]],
    reduce_prompt: str,
    allowed: set[str],
    pull_requests: int,
) -> dict[str, list[dict[str, Any]]]:
    """Merge every map recommendation in one call, then cap each category."""
    cap = recommendation_cap(pull_requests)
    by_id = {item["id"]: item for item in items}
    known = set(by_id)
    messages = build_reduce_messages(repo, items, system_prompt=reduce_prompt, max_recs=cap)
    response = factory.generate("synthesizer", messages, schema=Recommendations)
    ctx.emit(SynthesisChunk(text=response.text))
    reduced = _recommendations(response.parsed)
    reduced = _repair(factory, ctx, messages, response.text, reduced, known)
    merged = merge_same_title([item.model_dump() for item in reduced])
    unknown: set[str] = set()
    expanded: dict[str, list[dict[str, Any]]] = {}
    for category, group in group_by_category(merged).items():
        rows: list[dict[str, Any]] = []
        for item in group:
            sources = [source for source in item["sources"] if isinstance(source, str)]
            missing = [source for source in sources if source not in known]
            unknown.update(missing)
            pr_ids = _expand(sources, by_id, allowed)
            if not pr_ids:
                continue
            rows.append(
                {
                    "title": item["title"],
                    "risk": item["risk"],
                    "actions": item["actions"],
                    "pr_ids": pr_ids,
                }
            )
        if rows:
            expanded[category] = rows
    for source in sorted(unknown, key=_order):
        ctx.emit(CitationWarning(pr_id=source, message=_CITATION_WARN.format(pr_id=source)))
    if not expanded and unknown:
        raise ConfigError(_OUTSIDE)
    sections: dict[str, list[dict[str, Any]]] = {}
    for category, group in expanded.items():
        chosen = group[:cap]
        if not chosen:
            continue
        sections[category] = [
            {key: item[key] for key in ("title", "risk", "actions", "pr_ids")} for item in chosen
        ]
    return sections


def _cover(
    factory: LLMFactory,
    ctx: RunContext,
    repo: str,
    records: list[dict[str, Any]],
    map_prompt: str,
    sections: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """One follow-up when fewer than 80% of the input pull requests were cited."""
    cited = {
        pr_id
        for group in sections.values()
        for recommendation in group
        for pr_id in recommendation["pr_ids"]
    }
    if not records or len(cited) * 5 >= len(records) * 4:
        return sections
    missing = [record for record in records if record["pr_id"] not in cited]
    if not missing:
        return sections
    note = _coverage_note([record["pr_id"] for record in missing])
    extra = _split_cited(
        _map(factory, ctx, repo, missing, map_prompt, None, note=note)
    )
    if not extra:
        return sections
    flat = [
        {**recommendation, "owasp": category}
        for category, group in sections.items()
        for recommendation in group
    ]
    flat.extend(extra)
    merged: dict[str, list[dict[str, Any]]] = {}
    for category, group in group_by_category(flat).items():
        merged[category] = [
            {key: item[key] for key in ("title", "risk", "actions", "pr_ids")} for item in group
        ]
    return merged


def _coverage_note(pr_ids: list[str]) -> str:
    listed = ", ".join("#" + pr_id for pr_id in sorted(pr_ids, key=_order))
    return (
        "These pull requests were not cited: "
        f"{listed}. "
        "Add or extend recommendations for those that fit."
    )


def _expand(sources: list[str], by_id: dict[str, dict[str, Any]], allowed: set[str]) -> list[str]:
    pr_ids: list[str] = []
    seen: set[str] = set()
    for source in sources:
        origin = by_id.get(source)
        if origin is None:
            continue
        for pr_id in origin["pr_ids"]:
            if pr_id not in allowed or pr_id in seen:
                continue
            seen.add(pr_id)
            pr_ids.append(pr_id)
    return pr_ids


def _recommendations(parsed: Any) -> list[ReducedItem]:
    if not isinstance(parsed, Recommendations):
        raise ConfigError(_INVALID)
    return list(parsed.recommendations)


def _unknown_sources(items: list[ReducedItem], known: set[str]) -> set[str]:
    return {source for item in items for source in item.sources} - known


def _repair(
    factory: LLMFactory,
    ctx: RunContext,
    messages: list[Message],
    text: str,
    items: list[ReducedItem],
    known: set[str],
) -> list[ReducedItem]:
    """Ask once to drop source ids that are not in the map, then keep going."""
    if not items or not _unknown_sources(items, known):
        return items
    try:
        response = factory.generate(
            "synthesizer",
            [*messages, Message(role="user", content=_note(text, items, known))],
            schema=Recommendations,
        )
    except (ValidationError, ValueError):
        raise ConfigError(_INVALID) from None
    ctx.emit(SynthesisChunk(text=response.text))
    return _recommendations(response.parsed)


def _note(text: str, items: list[ReducedItem], known: set[str]) -> str:
    allowed = ", ".join(sorted(known, key=_order))
    rejected = ", ".join(sorted(_unknown_sources(items, known), key=_order))
    return (
        "Cite only recommendation ids from the input. "
        f"Allowed ids: {allowed}. "
        f"These ids are not in the input: {rejected}. "
        "Reply with JSON only, matching the requested schema.\n\n"
        f"{text}"
    )


def _attach_dates(ctx: RunContext, records: list[dict[str, Any]]) -> None:
    """Copy each cached pull request's update day onto the synthesis input."""
    for record in records:
        path = ctx.run_dir / "mine" / "prs" / f"{record['pr_id']}.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        updated = data.get("updated_at")
        if isinstance(updated, str) and len(updated) >= 10:
            record["date"] = updated[:10]


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
            actions = "\n".join(f"- {action}" for action in recommendation.actions)
            recommendations.append(
                f"**REC-{number:02d} — {recommendation.title}.**\n"
                f"{recommendation.risk}\n"
                f"{actions}\n"
                f"Related PRs: ({cites})"
            )
            number += 1
        blocks.append(f"## {index}. {topic.title}\n\n" + "\n\n".join(recommendations))
    lines.append("\n\n".join(blocks))
    return "\n".join(lines) + "\n"
