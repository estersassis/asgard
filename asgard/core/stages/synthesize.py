"""Turn consolidated security discussions into the User Guideline."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from asgard.core.config import ConfigError
from asgard.core.events import SynthesisChunk
from asgard.core.stage import RunContext, Stage
from asgard.prompts.synthesizer import build_messages
from asgard.providers.factory import LLMFactory
from asgard.providers.models import Message

_INPUT = "synthesize stage requires consolidated pull requests"
_FIELDS = ("pr_id", "title", "link", "evidence", "rationale")
_EMPTY = "There was no security discussion, so there is no guideline."
_OUTSIDE = "synthesis cites a pull request outside the input"
_INVALID = "synthesis response is invalid"


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
        messages = build_messages(self.repo, records, system_prompt=_override(ctx))
        response = self.factory.generate("synthesizer", messages, schema=Synthesis)
        ctx.emit(SynthesisChunk(text=response.text))
        guideline = _guideline(response.parsed)
        if _outside(guideline, allowed):
            note = _correction(response.text, guideline, allowed)
            try:
                response = self.factory.generate(
                    "synthesizer",
                    [*messages, Message(role="user", content=note)],
                    schema=Synthesis,
                )
            except (ValidationError, ValueError):
                raise ConfigError(_INVALID) from None
            ctx.emit(SynthesisChunk(text=response.text))
            guideline = _guideline(response.parsed)
            if _outside(guideline, allowed):
                raise ConfigError(_OUTSIDE)
        return _markdown(self.repo, guideline)


def _override(ctx: RunContext) -> str | None:
    path = ctx.run_dir / "synthesize" / "system_prompt.txt"
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8")


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


def _guideline(parsed: Any) -> Synthesis:
    if not isinstance(parsed, Synthesis):
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


def _correction(text: str, guideline: Synthesis, allowed: set[str]) -> str:
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
