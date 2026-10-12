"""Deterministic handler for tests. Each call consumes one scripted result."""

from asgard.providers.models import LLMResponse, Message


class FakeHandler:
    def __init__(self, script: list[str | Exception], model: str = "fake") -> None:
        self.script = list(script)
        self.model = model
        self.calls: list[list[Message]] = []

    def generate(
        self,
        messages: list[Message],
        *,
        schema: type | None = None,
        temperature: float = 0,
        timeout: float = 300,
    ) -> LLMResponse:
        self.calls.append(list(messages))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return LLMResponse(
            text=item,
            input_tokens=3,
            output_tokens=5,
            latency_s=0.01,
            model=self.model,
        )
