"""Versioned system prompts for classifier, evaluator, and synthesizer."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

ROLES = ("classifier", "evaluator", "synthesizer")
_DEFAULT_ID = "default"
_SLUG = re.compile(r"[^a-z0-9]+")


class PromptVersion(BaseModel):
    id: str
    name: str
    sha256: str
    created_at: str
    notes: str = ""
    body: str


class PromptRegistry:
    """User versions on disk plus a synthetic built-in ``default`` per role."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def list(self, role: str) -> list[PromptVersion]:
        _check_role(role)
        versions = [self.get(role, _DEFAULT_ID)]
        role_dir = self.root / role
        if not role_dir.is_dir():
            return versions
        extras: list[PromptVersion] = []
        for path in role_dir.glob("*.json"):
            if path.stem == _DEFAULT_ID:
                continue
            extras.append(self._load(path))
        extras.sort(key=lambda item: item.created_at, reverse=True)
        return versions + extras

    def get(self, role: str, version_id: str) -> PromptVersion:
        _check_role(role)
        if version_id == _DEFAULT_ID:
            body = builtin_body(role)
            return PromptVersion(
                id=_DEFAULT_ID,
                name=_DEFAULT_ID,
                sha256=content_sha256(body),
                created_at="",
                notes="Built-in system prompt",
                body=body,
            )
        path = self._path(role, version_id)
        if not path.is_file():
            raise LookupError(f"unknown {role} prompt: {version_id}")
        return self._load(path)

    def save(self, role: str, *, name: str, body: str, notes: str = "") -> PromptVersion:
        _check_role(role)
        name = name.strip()
        if not name:
            raise ValueError("prompt name must not be empty")
        version_id = self._allocate_id(role, name, body)
        if version_id == _DEFAULT_ID:
            raise ValueError("cannot overwrite the built-in default prompt")
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        version = PromptVersion(
            id=version_id,
            name=name,
            sha256=content_sha256(body),
            created_at=created_at,
            notes=notes,
            body=body,
        )
        path = self._path(role, version_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(version.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        return version

    def resolve(self, role: str, version_id: str) -> str:
        return self.get(role, version_id).body

    def _allocate_id(self, role: str, name: str, body: str) -> str:
        base = _slugify(name)
        digest = content_sha256(body)[:8]
        if base == _DEFAULT_ID:
            return f"prompt-{digest}"
        if not self._path(role, base).exists():
            return base
        candidate = f"{base}-{digest}"
        if not self._path(role, candidate).exists():
            return candidate
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        return f"{base}-{digest}-{stamp}"

    def _path(self, role: str, version_id: str) -> Path:
        return self.root / role / f"{version_id}.json"

    def _load(self, path: Path) -> PromptVersion:
        data = json.loads(path.read_text(encoding="utf-8"))
        return PromptVersion.model_validate(data)


def default_registry_root() -> Path:
    override = os.environ.get("ASGARD_PROMPTS_DIR")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".asgard" / "prompts"


def builtin_body(role: str) -> str:
    _check_role(role)
    if role == "classifier":
        from asgard.prompts.classifier import SYSTEM_PROMPT

        return SYSTEM_PROMPT
    if role == "evaluator":
        from asgard.prompts.evaluator import SYSTEM_PROMPT

        return SYSTEM_PROMPT
    from asgard.prompts.synthesizer import SYSTEM_PROMPT

    return SYSTEM_PROMPT


def adapt_prompt_body(role: str, body: str, *, batch: bool) -> str:
    """Return ``body`` as shown/used for solo or batch calls.

    For classifier/evaluator, ``batch=True`` maps the solo built-in onto the
    batch built-in (or wraps a custom solo prompt with the batch JSON envelope).
    """
    if not batch:
        return body
    if role == "classifier":
        from asgard.prompts.classifier import resolve_batch_system_prompt

        return resolve_batch_system_prompt(body)
    if role == "evaluator":
        from asgard.prompts.evaluator import resolve_batch_system_prompt

        return resolve_batch_system_prompt(body)
    return body


def content_sha256(body: str) -> str:
    return hashlib.sha256(body.encode()).hexdigest()


def resolve_system_prompt(role: str, config: object | None, run_dir: Path) -> str:
    """Return the active system prompt body for ``role``.

    Precedence:
    - synthesizer: ``synthesize/system_prompt.txt`` → registry selection → built-in
    - classifier / evaluator: registry selection → built-in

    ``config`` may be a ``RunConfig``, a redacted config dict, or ``None`` (defaults).
    """
    _check_role(role)
    if role == "synthesizer":
        override = Path(run_dir) / "synthesize" / "system_prompt.txt"
        if override.is_file():
            return override.read_text(encoding="utf-8")
    version_id, registry_dir = _selection(config, role)
    root = Path(registry_dir) if registry_dir is not None else default_registry_root()
    return PromptRegistry(root).resolve(role, version_id)


def _selection(config: object | None, role: str) -> tuple[str, Path | None]:
    if config is None:
        return _DEFAULT_ID, None
    prompts = getattr(config, "prompts", None)
    if prompts is None and isinstance(config, dict):
        prompts = config.get("prompts")
    if prompts is None:
        return _DEFAULT_ID, None
    if isinstance(prompts, dict):
        raw_id = prompts.get(role, _DEFAULT_ID)
        version_id = _DEFAULT_ID if raw_id is None else str(raw_id).strip() or _DEFAULT_ID
        raw_dir = prompts.get("registry_dir")
        registry_dir = Path(raw_dir) if raw_dir else None
        return version_id, registry_dir
    version_id = str(getattr(prompts, role, _DEFAULT_ID) or _DEFAULT_ID)
    registry_dir = getattr(prompts, "registry_dir", None)
    if registry_dir is not None:
        registry_dir = Path(registry_dir)
    return version_id, registry_dir


def _check_role(role: str) -> None:
    if role not in ROLES:
        raise ValueError(f"unknown prompt role: {role}")


def _slugify(name: str) -> str:
    slug = _SLUG.sub("-", name.strip().lower()).strip("-")
    return slug or "prompt"
