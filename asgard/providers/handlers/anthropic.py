"""Anthropic handler."""

import time

from anthropic import Anthropic

from asgard.providers.errors import ProviderError
from asgard.providers.models import LLMResponse, Message, response_schema

# Batch evaluation of many PRs needs headroom; 4k truncates mid-JSON.
_MAX_TOKENS = 16384


class AnthropicHandler:
    def __init__(self, model: str, api_key: str) -> None:
        self.model = model
        self._client = Anthropic(api_key=api_key)

    def generate(
        self,
        messages: list[Message],
        *,
        schema: type | None = None,
        temperature: float = 0,
        timeout: float = 300,
    ) -> LLMResponse:
        system = "\n".join(message.content for message in messages if message.role == "system")
        chat = [message.model_dump() for message in messages if message.role != "system"]
        # This SDK's Messages API has no temperature parameter.
        kwargs: dict = {
            "model": self.model,
            "max_tokens": _MAX_TOKENS,
            "messages": chat,
            "timeout": timeout,
        }
        if system:
            kwargs["system"] = system
        if schema is not None:
            kwargs["output_config"] = {
                "format": {"type": "json_schema", "schema": response_schema(schema)}
            }
        started = time.perf_counter()
        try:
            response = self._client.messages.create(**kwargs)
        except Exception as exc:
            raise ProviderError.from_exception(exc) from exc
        text = "".join(
            block.text for block in response.content if getattr(block, "type", None) == "text"
        )
        stop = getattr(response, "stop_reason", None)
        if stop == "max_tokens":
            raise ProviderError(
                f"anthropic output truncated at max_tokens ({_MAX_TOKENS})",
            )
        if stop == "refusal":
            raise ProviderError(text or "anthropic refused the request")
        if schema is not None and not text.strip():
            raise ProviderError(
                f"anthropic returned empty text (stop_reason={stop!r})",
            )
        usage = response.usage
        return LLMResponse(
            text=text,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            latency_s=time.perf_counter() - started,
            model=self.model,
        )
