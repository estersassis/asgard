"""Builds a handler per role and retries failed calls."""

import json
import logging
import os
import time
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel, ValidationError

from asgard.providers.errors import MissingAPIKey, ProviderError
from asgard.providers.models import (
    Credentials,
    LLMResponse,
    LLMSettings,
    Message,
    RoleName,
    RoleSpec,
    credentials_from_env,
)

logger = logging.getLogger("asgard.providers")
_ATTEMPTS = 3


class LLMFactory:
    def __init__(
        self,
        settings: LLMSettings,
        credentials: Credentials | None = None,
        *,
        handlers: dict[str, Any] | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.settings = settings
        self.credentials = credentials if credentials is not None else credentials_from_env()
        self.handlers = handlers or {}
        self.sleep = sleep

    def generate(
        self,
        role: RoleName,
        messages: list[Message],
        schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        spec = self.settings.resolve(role)
        handler = self._handler(spec)
        response = self._call(handler, spec, messages, schema)
        if schema is None:
            return response
        try:
            parsed = _validate(response.text, schema)
        except (ValueError, ValidationError):
            note = "Reply with JSON only, matching the requested schema.\n\n" + response.text
            repair = [*messages, Message(role="user", content=note)]
            response = self._call(handler, spec, repair, schema)
            parsed = _validate(response.text, schema)
        response.parsed = parsed
        return response

    def _handler(self, spec: RoleSpec) -> Any:
        injected = self.handlers.get(spec.provider)
        if injected is not None:
            return injected
        if spec.provider == "fake":
            raise ProviderError("fake provider requires an injected handler")
        api_key = None
        if spec.provider != "ollama":
            api_key = self.credentials.key_for(spec.provider)
            if not api_key:
                raise MissingAPIKey(spec.provider)
        return _build(spec.provider, spec.model, api_key)

    def _call(
        self,
        handler: Any,
        spec: RoleSpec,
        messages: list[Message],
        schema: Any,
    ) -> LLMResponse:
        error: ProviderError | None = None
        for attempt in range(_ATTEMPTS):
            try:
                response = handler.generate(
                    messages,
                    schema=schema,
                    temperature=spec.temperature,
                    timeout=spec.timeout,
                )
            except ProviderError as exc:
                error = exc
            except Exception as exc:
                error = ProviderError.from_exception(exc)
            else:
                _log(spec, response)
                return response
            if error is None or not _retryable(error) or attempt == _ATTEMPTS - 1:
                raise error
            self.sleep(_backoff(error))
        raise error  # pragma: no cover


def _validate(text: str, schema: type[BaseModel]) -> BaseModel:
    return schema.model_validate(_parse_json(text))


def _parse_json(text: str) -> Any:
    stripped = text.strip()
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass
    starts = [index for index in (stripped.find("{"), stripped.find("[")) if index != -1]
    if not starts:
        raise ValueError(_not_json(stripped))
    start = min(starts)
    end_char = "}" if stripped[start] == "{" else "]"
    end = stripped.rfind(end_char)
    if end <= start:
        raise ValueError(_not_json(stripped))
    try:
        return json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(_not_json(stripped)) from exc


def _not_json(text: str) -> str:
    preview = text.replace("\n", " ").strip()
    if len(preview) > 160:
        preview = preview[:160] + "…"
    if not preview:
        return "response is not JSON (empty)"
    return f"response is not JSON ({preview!r})"


def _retryable(error: ProviderError) -> bool:
    return error.timed_out or error.status in {408, 429, 503}


def _backoff(error: ProviderError) -> float:
    if error.status == 429:
        base = error.retry_after if error.retry_after is not None else 60
        return base + 10
    if error.status == 503:
        return 30
    return 10


def _log(spec: RoleSpec, response: LLMResponse) -> None:
    logger.info(
        "provider=%s model=%s latency_s=%.3f input_tokens=%s output_tokens=%s",
        spec.provider,
        response.model,
        response.latency_s,
        response.input_tokens,
        response.output_tokens,
    )


def _build(provider: str, model: str, api_key: str | None) -> Any:
    if provider == "gemini":
        from asgard.providers.handlers.gemini import GeminiHandler

        return GeminiHandler(model, api_key or "")
    if provider == "openai":
        from asgard.providers.handlers.openai import OpenAIHandler

        return OpenAIHandler(model, api_key or "")
    if provider == "anthropic":
        from asgard.providers.handlers.anthropic import AnthropicHandler

        return AnthropicHandler(model, api_key or "")
    if provider == "ollama":
        from asgard.providers.handlers.ollama import OllamaHandler

        host = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
        return OllamaHandler(model, host)
    raise ProviderError(f"unknown provider: {provider}")
