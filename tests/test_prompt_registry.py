"""Prompt registry versions and resolve precedence. No network."""

from pathlib import Path

import pytest

from asgard.core.prompt_registry import (
    PromptRegistry,
    builtin_body,
    content_sha256,
    default_registry_root,
    resolve_system_prompt,
)
from asgard.prompts.classifier import SYSTEM_PROMPT as CLASSIFIER_PROMPT


def test_default_is_synthetic_and_matches_module(tmp_path: Path) -> None:
    registry = PromptRegistry(tmp_path)
    version = registry.get("classifier", "default")
    assert version.id == "default"
    assert version.body == CLASSIFIER_PROMPT == builtin_body("classifier")
    assert version.sha256 == content_sha256(CLASSIFIER_PROMPT)
    assert not (tmp_path / "classifier" / "default.json").exists()


def test_save_list_get_and_resolve(tmp_path: Path) -> None:
    registry = PromptRegistry(tmp_path)
    body = "custom classifier body\n"
    saved = registry.save("classifier", name="Strict Rules", body=body, notes="pilot")

    assert saved.id == "strict-rules"
    assert saved.name == "Strict Rules"
    assert saved.body == body
    assert saved.sha256 == content_sha256(body)
    assert saved.notes == "pilot"
    assert saved.created_at.endswith("Z")
    assert (tmp_path / "classifier" / "strict-rules.json").is_file()

    listed = registry.list("classifier")
    assert listed[0].id == "default"
    assert [item.id for item in listed[1:]] == ["strict-rules"]
    assert registry.get("classifier", "strict-rules") == saved
    assert registry.resolve("classifier", "strict-rules") == body


def test_save_name_default_does_not_overwrite_builtin(tmp_path: Path) -> None:
    registry = PromptRegistry(tmp_path)
    body = "not the built-in\n"
    saved = registry.save("evaluator", name="default", body=body)

    assert saved.id.startswith("prompt-")
    assert saved.id != "default"
    assert registry.get("evaluator", "default").body == builtin_body("evaluator")
    assert registry.resolve("evaluator", saved.id) == body


def test_unknown_version_raises(tmp_path: Path) -> None:
    registry = PromptRegistry(tmp_path)
    with pytest.raises(LookupError, match="unknown classifier prompt"):
        registry.get("classifier", "missing")


def test_unknown_role_raises(tmp_path: Path) -> None:
    registry = PromptRegistry(tmp_path)
    with pytest.raises(ValueError, match="unknown prompt role"):
        registry.list("auditor")


def test_default_registry_root_env_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ASGARD_PROMPTS_DIR", str(tmp_path / "custom"))
    assert default_registry_root() == tmp_path / "custom"


def test_resolve_uses_registry_selection(tmp_path: Path) -> None:
    registry = PromptRegistry(tmp_path / "registry")
    body = "selected classifier\n"
    version = registry.save("classifier", name="alt", body=body)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    config = {
        "prompts": {
            "classifier": version.id,
            "registry_dir": str(registry.root),
        }
    }

    assert resolve_system_prompt("classifier", config, run_dir) == body
    assert resolve_system_prompt("evaluator", config, run_dir) == builtin_body("evaluator")


def test_resolve_synthesizer_run_dir_override_wins(tmp_path: Path) -> None:
    registry = PromptRegistry(tmp_path / "registry")
    version = registry.save("synthesizer", name="alt", body="from registry\n")
    run_dir = tmp_path / "run"
    override = run_dir / "synthesize" / "system_prompt.txt"
    override.parent.mkdir(parents=True)
    override.write_text("from run dir\n", encoding="utf-8")
    config = {
        "prompts": {
            "synthesizer": version.id,
            "registry_dir": str(registry.root),
        }
    }

    assert resolve_system_prompt("synthesizer", config, run_dir) == "from run dir\n"
