"""Live provider checks. Excluded from the default test run.

Run one provider when its key is available:

    GEMINI_API_KEY=... pytest tests/test_providers_smoke.py -m smoke
    OPENAI_API_KEY=... pytest tests/test_providers_smoke.py -m smoke
    ANTHROPIC_API_KEY=... pytest tests/test_providers_smoke.py -m smoke
    OLLAMA_SMOKE=1 pytest tests/test_providers_smoke.py -m smoke

Optional model overrides: GEMINI_MODEL, OPENAI_MODEL, ANTHROPIC_MODEL, OLLAMA_MODEL.
"""

import os

import pytest
from pydantic import BaseModel

from asgard.providers.factory import LLMFactory
from asgard.providers.models import LLMSettings, Message, RoleSpec

pytestmark = pytest.mark.smoke

MESSAGES = [Message(role="user", content='Reply with JSON {"label": "ok"}')]


class Classification(BaseModel):
    label: str


def _classification(provider: str, model: str) -> None:
    settings = LLMSettings(default=RoleSpec(provider=provider, model=model))
    response = LLMFactory(settings).generate("classifier", MESSAGES, schema=Classification)
    assert response.parsed.label
    assert response.latency_s >= 0
    assert response.input_tokens is not None
    assert response.output_tokens is not None
    assert response.model


@pytest.mark.skipif(
    not os.getenv("GEMINI_API_KEY"),
    reason="Set GEMINI_API_KEY to smoke-test Gemini",
)
def test_gemini_classification_call() -> None:
    _classification("gemini", os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))


@pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="Set OPENAI_API_KEY to smoke-test OpenAI",
)
def test_openai_classification_call() -> None:
    _classification("openai", os.getenv("OPENAI_MODEL", "gpt-4o-mini"))


@pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY"),
    reason="Set ANTHROPIC_API_KEY to smoke-test Anthropic",
)
def test_anthropic_classification_call() -> None:
    _classification("anthropic", os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-latest"))


@pytest.mark.skipif(
    os.getenv("OLLAMA_SMOKE") != "1",
    reason="Set OLLAMA_SMOKE=1 to smoke-test Ollama",
)
def test_ollama_classification_call() -> None:
    _classification("ollama", os.getenv("OLLAMA_MODEL", "llama3.1"))
