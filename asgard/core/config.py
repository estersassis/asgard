"""Stage 0: resolve a run's settings, check them, and save a redacted copy."""

import json
import os
import re
import tomllib
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator, model_validator

from asgard.providers.models import LLMSettings, RoleSpec, credentials_from_env

_ROLES = ("classifier", "evaluator", "synthesizer")
_REPO = re.compile(r"[^/\s]+/[^/\s]+")
_GITHUB = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$"
)


class ConfigError(Exception):
    """Invalid run settings. The message never includes a secret."""


class Filters(BaseModel):
    state: str = "all"
    merged_only: bool = False
    since: date | None = None
    until: date | None = None
    max_prs: int | None = None
    labels: list[str] = Field(default_factory=list)

    @field_validator("state")
    @classmethod
    def _state(cls, value: str) -> str:
        if value not in ("open", "closed", "all"):
            raise ValueError("state must be open, closed, or all")
        return value

    @model_validator(mode="after")
    def _bounds(self) -> "Filters":
        if self.since and self.until and self.since > self.until:
            raise ValueError("since is after until")
        if self.max_prs is not None and self.max_prs < 1:
            raise ValueError("max_prs must be >= 1")
        return self


class RunConfig(BaseModel):
    repo: str
    github_token: SecretStr = Field(exclude=True)
    llm: LLMSettings
    filters: Filters = Field(default_factory=Filters)
    max_iterations: int = 3
    prefilter: bool = False
    output_dir: Path

    @field_validator("repo")
    @classmethod
    def _repo(cls, value: str) -> str:
        match = _GITHUB.fullmatch(value.strip())
        if match:
            value = f"{match.group(1)}/{match.group(2)}"
        if not _REPO.fullmatch(value):
            raise ValueError("repo must be owner/repo or a github.com URL")
        return value

    @field_validator("max_iterations")
    @classmethod
    def _iterations(cls, value: int) -> int:
        if value < 1:
            raise ValueError("max_iterations must be >= 1")
        return value

    def redacted_dump(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


def load_dotenv(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def load_run(
    path: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> tuple[RunConfig, Any]:
    data: dict[str, Any] = _read(path) if path is not None else {}
    file_keys = data.pop("keys", None)
    file_token = data.pop("github_token", None)
    merged = _merge(data, overrides or {})
    if "output_dir" not in merged and "/" in str(merged.get("repo", "")):
        owner, name = str(merged["repo"]).split("/", 1)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        merged["output_dir"] = str(Path("runs") / f"{owner}__{name}" / stamp)
    if isinstance(merged.get("llm"), dict):
        merged["llm"] = _fill_roles(merged["llm"])
    token = os.environ.get("GITHUB_TOKEN") or file_token
    if not token:
        raise ConfigError("missing GITHUB_TOKEN")
    merged["github_token"] = token
    file_secrets = [str(item) for item in file_keys.values()] if isinstance(file_keys, dict) else []
    secrets = [token, *file_secrets]
    try:
        config = RunConfig.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(_redact(_validation_message(exc), secrets)) from None
    return config, credentials_from_env(file_keys if isinstance(file_keys, dict) else None)


def prepare_run(
    overrides: dict[str, Any] | None = None,
    config_path: Path | None = None,
    *,
    dotenv: Path | None = Path(".env"),
    opener: Any = None,
) -> RunConfig:
    if dotenv is not None:
        load_dotenv(dotenv)
    config, credentials = load_run(config_path, overrides)
    validate_run(config, credentials, opener=opener)
    target = config.output_dir / "config.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(config.redacted_dump(), indent=2) + "\n", encoding="utf-8")
    return config


def validate_run(config: RunConfig, credentials: Any, *, opener: Any = None) -> None:
    open_url = opener or urllib.request.urlopen
    secrets = _secret_values(config, credentials)
    _check_github(config, open_url, secrets)
    seen: list[RoleSpec] = []
    for spec in _specs(config):
        if spec.provider == "fake" or spec in seen:
            continue
        seen.append(spec)
        _check_provider(spec, credentials, open_url, secrets)


def _specs(config: RunConfig) -> list[RoleSpec]:
    specs = [config.llm.default]
    for role in _ROLES:
        spec = getattr(config.llm, role)
        if spec is not None:
            specs.append(spec)
    return specs


def _check_github(config: RunConfig, opener: Any, secrets: list[str]) -> None:
    owner, name = config.repo.split("/", 1)
    url = f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
    token = config.github_token.get_secret_value()
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "asgard",
        },
    )
    with _open(opener, request, secrets, "github") as response:
        scopes = response.headers.get("X-OAuth-Scopes")
    if not scopes or not str(scopes).strip():
        return
    names = {item.strip() for item in str(scopes).split(",")}
    if "repo" not in names and "public_repo" not in names:
        raise ConfigError("token is missing the repo scope")


def _check_provider(spec: RoleSpec, credentials: Any, opener: Any, secrets: list[str]) -> None:
    model = quote(spec.model, safe="")
    if spec.provider == "ollama":
        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
        request = urllib.request.Request(f"{host}/api/tags", headers={"User-Agent": "asgard"})
    elif spec.provider == "openai":
        request = urllib.request.Request(
            f"https://api.openai.com/v1/models/{model}",
            headers={
                "Authorization": f"Bearer {_key(credentials, 'openai')}",
                "User-Agent": "asgard",
            },
        )
    elif spec.provider == "gemini":
        request = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}",
            headers={"x-goog-api-key": _key(credentials, "gemini"), "User-Agent": "asgard"},
        )
    elif spec.provider == "anthropic":
        request = urllib.request.Request(
            f"https://api.anthropic.com/v1/models/{model}",
            headers={
                "x-api-key": _key(credentials, "anthropic"),
                "anthropic-version": "2023-06-01",
                "User-Agent": "asgard",
            },
        )
    else:
        raise ConfigError(f"unknown provider: {spec.provider}")
    with _open(opener, request, secrets, spec.provider):
        return


def _key(credentials: Any, provider: str) -> str:
    key = credentials.key_for(provider)
    if not key:
        raise ConfigError(f"missing API key for {provider}")
    return key


def _open(opener: Any, request: urllib.request.Request, secrets: list[str], label: str) -> Any:
    try:
        return opener(request, timeout=10)
    except urllib.error.HTTPError as exc:
        raise ConfigError(_http_message(label, exc, secrets)) from None
    except urllib.error.URLError as exc:
        raise ConfigError(_redact(f"{label} is not reachable: {exc.reason}", secrets)) from None


def _http_message(label: str, exc: urllib.error.HTTPError, secrets: list[str]) -> str:
    if label == "github" and exc.code == 401:
        return "invalid GitHub token"
    if label == "github" and exc.code == 404:
        return "repository not found or token cannot see it"
    detail = ""
    if exc.fp is not None:
        detail = exc.read().decode(errors="replace")[:200]
    return _redact(f"{label} is not reachable: HTTP {exc.code} {detail}".strip(), secrets)


def _secret_values(config: RunConfig, credentials: Any) -> list[str]:
    values = [config.github_token.get_secret_value()]
    for provider in ("gemini", "openai", "anthropic"):
        key = credentials.key_for(provider)
        if key:
            values.append(key)
    return values


def _validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(item) for item in err["loc"])
        text = str(err["msg"]).removeprefix("Value error, ")
        parts.append(f"{loc}: {text}" if loc else text)
    return "; ".join(parts)


def _redact(message: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            message = message.replace(secret, "***")
    return message


def _read(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        data = json.loads(text)
    elif path.suffix == ".toml":
        data = tomllib.loads(text)
    else:
        raise ConfigError(f"unsupported settings file: {path.suffix}")
    if not isinstance(data, dict):
        raise ConfigError("settings file must be a table")
    return data


def _merge(base: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in overrides.items():
        if value is None:
            continue
        current = merged.get(key)
        if isinstance(value, dict) and isinstance(current, dict):
            merged[key] = _merge(current, value)
        else:
            merged[key] = value
    return merged


def _fill_roles(llm: dict[str, Any]) -> dict[str, Any]:
    default = llm.get("default") or {}
    filled = dict(llm)
    for role in _ROLES:
        spec = filled.get(role)
        if not isinstance(spec, dict):
            continue
        spec = dict(spec)
        if default.get("provider"):
            spec.setdefault("provider", default["provider"])
        if default.get("model"):
            spec.setdefault("model", default["model"])
        filled[role] = spec
    return filled
