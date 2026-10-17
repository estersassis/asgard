"""Pilot set schema and loader for bundled and on-disk sets."""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from asgard.core.normalization import NormalizedPullRequest

BUNDLED_SETS = frozenset({"example"})


class PilotItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: bool
    pr: NormalizedPullRequest


class PilotSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    items: list[PilotItem] = Field(min_length=1)


def load_pilot_set(source: str | Path) -> PilotSet:
    """Load a pilot set by bundled id or filesystem path.

    ``source`` may be a known bundled id (e.g. ``example``) or a path to a
    JSON file matching the pilot-set schema. Each PR is validated as a
    :class:`~asgard.core.normalization.NormalizedPullRequest`.
    """
    raw = json.loads(_read_text(source))
    return PilotSet.model_validate(raw)


def _read_text(source: str | Path) -> str:
    if isinstance(source, Path):
        return source.read_text(encoding="utf-8")

    text = source.strip()
    if not text:
        raise ValueError("pilot set source must not be empty")

    if text in BUNDLED_SETS:
        return (
            files("asgard.data.pilot")
            .joinpath(f"{text}.json")
            .read_text(encoding="utf-8")
        )

    path = Path(text)
    if path.is_file():
        return path.read_text(encoding="utf-8")

    raise LookupError(
        f"unknown pilot set: {text!r} "
        f"(known bundled: {', '.join(sorted(BUNDLED_SETS))})"
    )
