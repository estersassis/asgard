"""Pipeline helpers: clear_from preserves the synthesizer prompt override."""

import json
from pathlib import Path

import pytest

from asgard.core.pipeline import (
    PIPELINE,
    classify_error_ids,
    clear_from,
    drop_classify_items,
)


def _write(path: Path, text: str = "x\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_clear_from_removes_later_stages_but_keeps_prompt(tmp_path: Path) -> None:
    for name in PIPELINE:
        _write(tmp_path / name / "output.json", f'"{name}"\n')
    prompt = "Keep {repository}."
    _write(tmp_path / "synthesize" / "system_prompt.txt", prompt)
    _write(tmp_path / "SECURITY.md", "guideline\n")
    _write(tmp_path / "manifest.json", "{}\n")
    _write(tmp_path / "consolidate" / "output.json", "[]\n")

    clear_from(tmp_path, "synthesize")

    assert (tmp_path / "consolidate" / "output.json").is_file()
    assert not (tmp_path / "synthesize" / "output.json").exists()
    assert not (tmp_path / "security_md").exists()
    assert not (tmp_path / "bundle").exists()
    assert not (tmp_path / "SECURITY.md").exists()
    assert not (tmp_path / "manifest.json").exists()
    kept = tmp_path / "synthesize" / "system_prompt.txt"
    assert kept.is_file()
    assert kept.read_text(encoding="utf-8") == prompt


def test_clear_from_rejects_unknown_stage(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="unknown stage"):
        clear_from(tmp_path, "forge")


def test_drop_classify_items_keeps_successes_and_clears_later(tmp_path: Path) -> None:
    classify = tmp_path / "classify"
    classify.mkdir()
    rows = [
        {"id": "1", "payload": {"pr_id": "1", "status": "approved"}},
        {"id": "2", "payload": {"pr_id": "2", "status": "error"}},
        {"id": "3", "payload": {"pr_id": "3", "status": "error"}},
    ]
    (classify / "items.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    _write(classify / "output.json", "[]\n")
    _write(classify / "metrics.json", "{}\n")
    _write(tmp_path / "consolidate" / "output.json", "[]\n")
    _write(tmp_path / "bundle" / "output.json", "{}\n")

    assert classify_error_ids(tmp_path) == ["2", "3"]
    removed = drop_classify_items(tmp_path, ["2", "3", "missing"])
    assert removed == ["2", "3"]

    kept = [
        json.loads(line)
        for line in (classify / "items.jsonl").read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert kept == [rows[0]]
    assert not (classify / "output.json").exists()
    assert not (classify / "metrics.json").exists()
    assert (classify / "items.jsonl").is_file()
    assert not (tmp_path / "consolidate").exists()
    assert not (tmp_path / "bundle").exists()
