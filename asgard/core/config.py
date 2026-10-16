"""Stage 0: resolve a run's settings, check them, and save a redacted copy."""

import json
import os
import re
import tomllib
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import quote

from pydantic import BaseModel, Field, SecretStr, ValidationError, field_validator, model_validator

from asgard.providers.models import LLMSettings, RoleSpec, credentials_from_env

_ROLES = ("classifier", "evaluator", "synthesizer")
_REPO = re.compile(r"[^/\s]+/[^/\s]+")
_GITHUB = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?$"
)
_SECRET_ENV = (
    "GITHUB_TOKEN",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OLLAMA_HOST",
)
_TOP_LEVEL = (
    "repo",
    "max_iterations",
    "batch_size",
    "batch_max_chars",
    "prefilter",
    "include_unresolved",
    "export_xlsx",
    "output_dir",
    "security_md",
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


class MiningSettings(BaseModel):
    page_size: int = 50

    @field_validator("page_size")
    @classmethod
    def _page_size(cls, value: int) -> int:
        if value < 1:
            raise ValueError("page_size must be >= 1")
        return value


class RunConfig(BaseModel):
    repo: str
    github_token: SecretStr = Field(exclude=True)
    llm: LLMSettings
    filters: Filters = Field(default_factory=Filters)
    mining: MiningSettings = Field(default_factory=MiningSettings)
    max_iterations: int = 3
    batch_size: int = 1
    batch_max_chars: int | None = None
    prefilter: bool = False
    include_unresolved: bool = False
    export_xlsx: bool = False
    output_dir: Path
    security_md: Path | None = None

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

    @field_validator("batch_size")
    @classmethod
    def _batch_size(cls, value: int) -> int:
        if value < 1:
            raise ValueError("batch_size must be >= 1")
        return value

    @field_validator("batch_max_chars")
    @classmethod
    def _batch_max_chars(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("batch_max_chars must be >= 1")
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


@contextmanager
def _secret_env(secrets: dict[str, str] | None, *, restore: bool = True) -> Iterator[None]:
    """Apply overlay env vars. Prefer overlay over existing env / file keys."""
    if not secrets:
        yield
        return
    previous: dict[str, str | None] = {}
    for key, value in secrets.items():
        if key not in _SECRET_ENV or value is None or value == "":
            continue
        previous[key] = os.environ.get(key)
        os.environ[key] = str(value)
    try:
        yield
    finally:
        if not restore:
            return
        for key, old in previous.items():
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old


def _peel_secrets(
    overrides: dict[str, Any] | None,
) -> tuple[dict[str, Any], str | None, dict[str, str] | None]:
    peeled = dict(overrides or {})
    token = peeled.pop("github_token", None)
    keys = peeled.pop("keys", None)
    if token is not None:
        token = str(token)
    if keys is not None and not isinstance(keys, dict):
        raise ConfigError("keys must be a table")
    if isinstance(keys, dict):
        keys = {str(name): str(value) for name, value in keys.items()}
    return peeled, token, keys


def load_run(
    path: Path | None = None,
    overrides: dict[str, Any] | None = None,
    secrets: dict[str, str] | None = None,
) -> tuple[RunConfig, Any]:
    with _secret_env(secrets):
        return _load_run(path, overrides)


def _load_run(
    path: Path | None,
    overrides: dict[str, Any] | None,
) -> tuple[RunConfig, Any]:
    data: dict[str, Any] = _read(path) if path is not None else {}
    file_keys = data.pop("keys", None)
    file_token = data.pop("github_token", None)
    peeled, override_token, override_keys = _peel_secrets(overrides)
    merged = _merge(data, peeled)
    if "output_dir" not in merged and "/" in str(merged.get("repo", "")):
        owner, name = str(merged["repo"]).split("/", 1)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        merged["output_dir"] = str(Path("runs") / f"{owner}__{name}" / stamp)
    if isinstance(merged.get("llm"), dict):
        merged["llm"] = _fill_roles(merged["llm"])
    token = os.environ.get("GITHUB_TOKEN") or override_token or file_token
    if not token:
        raise ConfigError("missing GITHUB_TOKEN")
    merged["github_token"] = token
    key_table: dict[str, str] = {}
    if isinstance(file_keys, dict):
        key_table.update({str(k): str(v) for k, v in file_keys.items()})
    if override_keys:
        key_table.update(override_keys)
    file_secrets = list(key_table.values())
    redact_list = [token, *file_secrets]
    try:
        config = RunConfig.model_validate(merged)
    except ValidationError as exc:
        raise ConfigError(_redact(_validation_message(exc), redact_list)) from None
    return config, credentials_from_env(key_table or None)


def prepare_run(
    overrides: dict[str, Any] | None = None,
    config_path: Path | None = None,
    *,
    dotenv: Path | None = Path(".env"),
    opener: Any = None,
    secrets: dict[str, str] | None = None,
) -> RunConfig:
    if dotenv is not None:
        load_dotenv(dotenv)
    # Keep overlay env for this process so the pipeline/provider factory sees keys / OLLAMA_HOST.
    with _secret_env(secrets, restore=False):
        config, credentials = _load_run(config_path, overrides)
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


def probe_run(config: RunConfig, credentials: Any, *, opener: Any = None) -> dict[str, Any]:
    """Collect per-target reachability results instead of fail-fast."""
    open_url = opener or urllib.request.urlopen
    secrets = _secret_values(config, credentials)
    results: dict[str, Any] = {}
    try:
        _check_github(config, open_url, secrets)
        results["github"] = {"ok": True, "message": ""}
    except ConfigError as exc:
        results["github"] = {"ok": False, "message": str(exc)}
    seen: list[RoleSpec] = []
    for spec in _specs(config):
        if spec.provider == "fake" or spec in seen:
            continue
        seen.append(spec)
        try:
            _check_provider(spec, credentials, open_url, secrets)
            results[spec.provider] = {"ok": True, "message": ""}
        except ConfigError as exc:
            results[spec.provider] = {"ok": False, "message": str(exc)}
    return results


def dump_toml(data: dict[str, Any]) -> str:
    """Serialize a RunConfig-shaped dict to TOML. Never writes secrets."""
    payload = dict(data)
    payload.pop("github_token", None)
    payload.pop("keys", None)
    lines: list[str] = []

    for key in _TOP_LEVEL:
        if key not in payload or payload[key] is None:
            continue
        lines.append(f"{key} = {_toml_value(payload[key])}")

    filters = payload.get("filters")
    if isinstance(filters, dict) and filters:
        if lines:
            lines.append("")
        lines.append("[filters]")
        for key, value in filters.items():
            if value is None:
                continue
            lines.append(f"{key} = {_toml_value(value)}")

    mining = payload.get("mining")
    if isinstance(mining, dict) and mining:
        if lines:
            lines.append("")
        lines.append("[mining]")
        for key, value in mining.items():
            if value is None:
                continue
            lines.append(f"{key} = {_toml_value(value)}")

    llm = payload.get("llm")
    if isinstance(llm, dict):
        default = llm.get("default")
        if isinstance(default, dict) and default:
            if lines:
                lines.append("")
            lines.append("[llm.default]")
            for key, value in default.items():
                if value is None:
                    continue
                lines.append(f"{key} = {_toml_value(value)}")
        else:
            default = {}
        for role in _ROLES:
            spec = llm.get(role)
            if not isinstance(spec, dict) or not spec:
                continue
            if str(spec.get("provider", "")) == "default":
                continue
            trimmed = _role_for_dump(spec, default if isinstance(default, dict) else {})
            if not trimmed:
                continue
            if lines:
                lines.append("")
            lines.append(f"[llm.{role}]")
            for key, value in trimmed.items():
                lines.append(f"{key} = {_toml_value(value)}")

    return "\n".join(lines) + ("\n" if lines else "")


def _role_for_dump(spec: dict[str, Any], default: dict[str, Any]) -> dict[str, Any]:
    """Omit a role that fully matches default so inheritance matches `_fill_roles`."""
    out: dict[str, Any] = {}
    for key, value in spec.items():
        if value is None:
            continue
        if key in default and value == default[key]:
            continue
        out[key] = value
    if not out:
        return {}
    # Still identical to default after fill → omit entirely.
    provider = out.get("provider", default.get("provider"))
    model = out.get("model", default.get("model"))
    temperature = out.get("temperature", default.get("temperature", 0))
    timeout = out.get("timeout", default.get("timeout", 300))
    if (
        provider == default.get("provider")
        and model == default.get("model")
        and temperature == default.get("temperature", 0)
        and timeout == default.get("timeout", 300)
        and set(out) <= {"provider", "model", "temperature", "timeout"}
    ):
        return {}
    return out


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, datetime):
        return _toml_value(value.date().isoformat())
    if isinstance(value, date):
        return _toml_value(value.isoformat())
    if isinstance(value, Path):
        return _toml_value(str(value))
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


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
        with _open(opener, request, secrets, spec.provider) as response:
            body = response.read() if hasattr(response, "read") else b"{}"
            _require_ollama_model(body, spec.model)
        return
    if spec.provider == "openai":
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


def _require_ollama_model(body: bytes, model: str) -> None:
    try:
        payload = json.loads(body.decode() if isinstance(body, bytes) else body or "{}")
    except (TypeError, ValueError, UnicodeDecodeError):
        payload = {}
    names: set[str] = set()
    for item in payload.get("models", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("model")
        if name:
            names.add(str(name))
    if _ollama_has_model(names, model):
        return
    raise ConfigError(f"ollama model is not pulled: {model}")


def _ollama_has_model(names: set[str], model: str) -> bool:
    if model in names:
        return True
    if ":" not in model and f"{model}:latest" in names:
        return True
    base = model.split(":", 1)[0]
    return any(name == model or name.split(":", 1)[0] == base for name in names)


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
