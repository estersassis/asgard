"""Provider settings and the result of one LLM call."""

import json
import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, SecretStr

RoleName = Literal["classifier", "evaluator", "synthesizer", "simplifier"]
ProviderName = Literal["gemini", "openai", "anthropic", "ollama", "fake"]

_ENV_KEYS = {
    "gemini": "GEMINI_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class LLMResponse(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    text: str
    parsed: Any = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_s: float
    model: str


class RoleSpec(BaseModel):
    provider: ProviderName
    model: str
    temperature: float = 0
    timeout: float = 300


class LLMSettings(BaseModel):
    default: RoleSpec
    classifier: RoleSpec | None = None
    evaluator: RoleSpec | None = None
    synthesizer: RoleSpec | None = None
    simplifier: RoleSpec | None = None

    def resolve(self, role: RoleName) -> RoleSpec:
        if role == "simplifier":
            return self.simplifier or self.synthesizer or self.default
        if role not in ("classifier", "evaluator", "synthesizer"):
            raise ValueError(f"unknown role: {role}")
        return getattr(self, role) or self.default


class Credentials(BaseModel):
    gemini: SecretStr | None = None
    openai: SecretStr | None = None
    anthropic: SecretStr | None = None

    def key_for(self, provider: str) -> str | None:
        secret = getattr(self, provider, None)
        if not isinstance(secret, SecretStr):
            return None
        return secret.get_secret_value()


def credentials_from_env(file_keys: dict[str, str] | None = None) -> Credentials:
    file_keys = file_keys or {}
    values: dict[str, SecretStr | None] = {}
    for provider, env_name in _ENV_KEYS.items():
        raw = os.environ.get(env_name) or file_keys.get(provider) or None
        values[provider] = SecretStr(raw) if raw else None
    return Credentials(**values)


def load_settings(path: Path) -> tuple[LLMSettings, Credentials]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        data = json.loads(text)
    elif path.suffix == ".toml":
        data = tomllib.loads(text)
    else:
        raise ValueError(f"unsupported settings file: {path.suffix}")
    file_keys = data.pop("keys", None)
    return LLMSettings.model_validate(data), credentials_from_env(file_keys)


def response_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()
    schema.pop("$schema", None)
    if schema.get("type") == "object":
        schema.setdefault("additionalProperties", False)
    return schema
