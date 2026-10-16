"""List and save versioned system prompts via the prompt registry."""

from __future__ import annotations

from pathlib import Path

from asgard.core.prompt_registry import ROLES, PromptRegistry, default_registry_root


def list_prompts(*, role: str | None = None, registry_dir: Path | None = None) -> None:
    """Print registry versions for one role, or all roles when ``role`` is omitted."""
    registry = PromptRegistry(registry_dir or default_registry_root())
    roles = (role,) if role else ROLES
    for current in roles:
        for version in registry.list(current):
            notes = version.notes.replace("\n", " ").strip()
            created = version.created_at or "-"
            print(
                f"{current}\t{version.id}\t{version.sha256}\t{created}"
                + (f"\t{notes}" if notes else "")
            )


def save_prompt(
    *,
    role: str,
    name: str,
    file: Path,
    notes: str = "",
    registry_dir: Path | None = None,
) -> None:
    """Read ``file`` and save it as a new version; print the saved id and sha256."""
    body = file.read_text(encoding="utf-8")
    registry = PromptRegistry(registry_dir or default_registry_root())
    version = registry.save(role, name=name, body=body, notes=notes)
    print(f"{version.id}\t{version.sha256}")
