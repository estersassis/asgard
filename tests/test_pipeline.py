"""Pipeline helpers: clear_from preserves the synthesizer prompt override."""

from pathlib import Path

import pytest

from asgard.core.pipeline import PIPELINE, clear_from


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
