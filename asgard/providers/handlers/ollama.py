"""Local Ollama handler. Uses the chat API so messages stay in one shape."""

import json
import time
import urllib.error
import urllib.request

from asgard.providers.errors import ProviderError
from asgard.providers.models import LLMResponse, Message, response_schema


class OllamaHandler:
    def __init__(self, model: str, host: str = "http://localhost:11434") -> None:
        self.model = model
        self.host = host.rstrip("/")

    def generate(
        self,
        messages: list[Message],
        *,
        schema: type | None = None,
        temperature: float = 0,
        timeout: float = 300,
    ) -> LLMResponse:
        payload: dict = {
            "model": self.model,
            "messages": [message.model_dump() for message in messages],
            "stream": False,
            "options": {"temperature": temperature},
        }
        if schema is not None:
            payload["format"] = response_schema(schema)
        body = json.dumps(payload).encode()
        request = urllib.request.Request(
            f"{self.host}/api/chat",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise ProviderError(detail or str(exc), status=exc.code) from exc
        except Exception as exc:
            raise ProviderError.from_exception(exc) from exc
        text = (data.get("message") or {}).get("content") or ""
        return LLMResponse(
            text=text,
            input_tokens=data.get("prompt_eval_count"),
            output_tokens=data.get("eval_count"),
            latency_s=time.perf_counter() - started,
            model=self.model,
        )
