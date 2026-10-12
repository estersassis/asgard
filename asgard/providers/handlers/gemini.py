"""Gemini handler. The caller supplies messages; this module only calls the API."""

import time

from google import genai
from google.genai import types

from asgard.providers.errors import ProviderError
from asgard.providers.models import LLMResponse, Message, response_schema


class GeminiHandler:
    def __init__(self, model: str, api_key: str) -> None:
        self.model = model
        self._client = genai.Client(api_key=api_key)

    def generate(
        self,
        messages: list[Message],
        *,
        schema: type | None = None,
        temperature: float = 0,
        timeout: float = 300,
    ) -> LLMResponse:
        system = "\n".join(message.content for message in messages if message.role == "system")
        contents = [
            types.Content(
                role="model" if message.role == "assistant" else "user",
                parts=[types.Part(text=message.content)],
            )
            for message in messages
            if message.role != "system"
        ]
        config: dict = {
            "temperature": temperature,
            "http_options": types.HttpOptions(timeout=int(timeout * 1000)),
        }
        if system:
            config["system_instruction"] = system
        if schema is not None:
            config["response_mime_type"] = "application/json"
            config["response_schema"] = response_schema(schema)
        started = time.perf_counter()
        try:
            response = self._client.models.generate_content(
                model=self.model,
                contents=contents,
                config=types.GenerateContentConfig(**config),
            )
        except Exception as exc:
            raise ProviderError.from_exception(exc) from exc
        usage = getattr(response, "usage_metadata", None)
        return LLMResponse(
            text=response.text or "",
            input_tokens=getattr(usage, "prompt_token_count", None),
            output_tokens=getattr(usage, "candidates_token_count", None),
            latency_s=time.perf_counter() - started,
            model=self.model,
        )
