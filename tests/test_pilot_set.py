"""Bundled pilot set loader and schema checks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from asgard.core.normalization import NormalizedPullRequest
from asgard.core.pilot.set import BUNDLED_SETS, PilotSet, load_pilot_set


def test_bundled_example_loads_with_valid_prs() -> None:
    assert "example" in BUNDLED_SETS
    pilot = load_pilot_set("example")
    assert isinstance(pilot, PilotSet)
    assert pilot.id == "example"
    assert len(pilot.items) == 4
    assert sum(1 for item in pilot.items if item.label) == 2
    assert sum(1 for item in pilot.items if not item.label) == 2
    for item in pilot.items:
        assert isinstance(item.pr, NormalizedPullRequest)
        assert item.pr.context.pr_id
        assert item.pr.schema_version == "1"


def test_bundled_binary_v2_loads_with_valid_prs() -> None:
    assert "binary_v2" in BUNDLED_SETS
    pilot = load_pilot_set("binary_v2")
    assert isinstance(pilot, PilotSet)
    assert pilot.id == "binary_v2"
    assert len(pilot.items) == 15
    assert sum(1 for item in pilot.items if item.label) == 7
    assert sum(1 for item in pilot.items if not item.label) == 8
    for item in pilot.items:
        assert isinstance(item.pr, NormalizedPullRequest)
        assert item.pr.context.pr_id.startswith("pilot-")
        assert item.pr.schema_version == "1"


def test_load_pilot_set_from_path(tmp_path: Path) -> None:
    bundled = load_pilot_set("example")
    path = tmp_path / "custom.json"
    path.write_text(
        json.dumps(bundled.model_dump(mode="json"), indent=2) + "\n",
        encoding="utf-8",
    )
    loaded = load_pilot_set(path)
    assert loaded.id == bundled.id
    assert len(loaded.items) == len(bundled.items)
    again = load_pilot_set(str(path))
    assert again == loaded


def test_unknown_and_empty_pilot_set() -> None:
    with pytest.raises(LookupError, match="unknown pilot set"):
        load_pilot_set("django_vem_missing")
    with pytest.raises(ValueError, match="must not be empty"):
        load_pilot_set("   ")
