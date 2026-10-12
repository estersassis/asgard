"""OpenAI handler."""

import time

from openai import OpenAI

from asgard.providers.errors import ProviderError
from asgard.providers.models import LLMResponse, Message, response_schema


class OpenAIHandler:
    def __init__(self, model: str, api_key: str) -> None:
        self.model = model
        self._client = OpenAI(api_key=api_key)

    def generate(
        self,
        messages: list[Message],
        *,
        schema: type | None = None,
        temperature: float = 0,
        timeout: float = 300,
    ) -> LLMResponse:
        kwargs: dict = {
            "model": self.model,
            "messages": [message.model_dump() for message in messages],
            "temperature": temperature,
            "timeout": timeout,
        }
        if schema is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.__name__,
                    "strict": True,
                    "schema": response_schema(schema),
                },
            }
        started = time.perf_counter()
        try:
            completion = self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            raise ProviderError.from_exception(exc) from exc
        message = completion.choices[0].message
        usage = completion.usage
        return LLMResponse(
            text=message.content or "",
            input_tokens=getattr(usage, "prompt_tokens", None),
            output_tokens=getattr(usage, "completion_tokens", None),
            latency_s=time.perf_counter() - started,
            model=self.model,
        )
