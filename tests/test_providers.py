"""Provider factory, role settings, and retries. No network."""

import ast
import logging
from pathlib import Path

import pytest
from pydantic import BaseModel

from asgard.providers.errors import MissingAPIKey, ProviderError
from asgard.providers.factory import LLMFactory
from asgard.providers.handlers.fake import FakeHandler
from asgard.providers.models import (
    Credentials,
    LLMSettings,
    Message,
    RoleSpec,
    credentials_from_env,
    load_settings,
)

PROVIDERS_ROOT = Path(__file__).resolve().parents[1] / "asgard" / "providers"

MESSAGES = [Message(role="user", content='Reply with JSON {"label": "ok"}')]


class Classification(BaseModel):
    label: str


def _factory(
    handler: FakeHandler,
    sleep: list[float] | None = None,
    **overrides: RoleSpec,
) -> LLMFactory:
    settings = LLMSettings(default=RoleSpec(provider="fake", model="fake"), **overrides)
    return LLMFactory(
        settings,
        Credentials(),
        handlers={"fake": handler},
        sleep=(sleep.append if sleep is not None else lambda _: None),
    )


def test_classification_records_usage() -> None:
    handler = FakeHandler(['{"label": "ok"}'])
    response = _factory(handler).generate("classifier", MESSAGES, schema=Classification)
    assert response.parsed.label == "ok"
    assert response.input_tokens == 3
    assert response.output_tokens == 5
    assert response.latency_s == 0.01
    assert response.model == "fake"


def test_unspecified_role_uses_default_and_override_switches_model() -> None:
    settings = LLMSettings(
        default=RoleSpec(provider="fake", model="base"),
        evaluator=RoleSpec(provider="ollama", model="llama"),
    )
    assert settings.resolve("classifier").model == "base"
    assert settings.resolve("synthesizer").provider == "fake"
    assert settings.resolve("evaluator") == RoleSpec(provider="ollama", model="llama")

    default_handler = FakeHandler(['{"label": "ok"}'], model="base")
    evaluator_handler = FakeHandler(['{"label": "eval"}'], model="llama")
    factory = LLMFactory(
        settings,
        Credentials(),
        handlers={"fake": default_handler, "ollama": evaluator_handler},
        sleep=lambda _: None,
    )
    classified = factory.generate("classifier", MESSAGES, schema=Classification)
    evaluated = factory.generate("evaluator", MESSAGES, schema=Classification)
    assert classified.parsed.label == "ok"
    assert evaluated.parsed.label == "eval"
    assert len(default_handler.calls) == 1
    assert len(evaluator_handler.calls) == 1


def test_retries_rate_limit_then_stops() -> None:
    slept: list[float] = []
    handler = FakeHandler(
        [
            ProviderError("retry in 2", status=429, retry_after=2),
            '{"label": "ok"}',
        ]
    )
    response = _factory(handler, slept).generate("classifier", MESSAGES, schema=Classification)
    assert response.parsed.label == "ok"
    assert slept == [12]
    assert len(handler.calls) == 2

    exhausted = FakeHandler([ProviderError("rate", status=429) for _ in range(3)])
    slept.clear()
    with pytest.raises(ProviderError):
        _factory(exhausted, slept).generate("classifier", MESSAGES, schema=Classification)
    assert len(exhausted.calls) == 3
    assert slept == [70, 70]


def test_backoff_for_timeout_and_unavailable() -> None:
    slept: list[float] = []
    handler = FakeHandler(
        [
            ProviderError("slow", timed_out=True),
            ProviderError("down", status=503),
            '{"label": "ok"}',
        ]
    )
    response = _factory(handler, slept).generate("classifier", MESSAGES, schema=Classification)
    assert response.parsed.label == "ok"
    assert slept == [10, 30]


def test_other_status_does_not_retry() -> None:
    slept: list[float] = []
    handler = FakeHandler([ProviderError("nope", status=400), '{"label": "ok"}'])
    with pytest.raises(ProviderError):
        _factory(handler, slept).generate("classifier", MESSAGES, schema=Classification)
    assert len(handler.calls) == 1
    assert slept == []


def test_invalid_json_is_repaired_once() -> None:
    handler = FakeHandler(["not json", 'note {"label": "ok"}'])
    response = _factory(handler).generate("classifier", MESSAGES, schema=Classification)
    assert response.parsed.label == "ok"
    assert len(handler.calls) == 2
    assert handler.calls[1][-1].content.startswith("Reply with JSON only")


def test_secrets_stay_out_of_logs_and_repr(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    secret = "super-secret-key"
    monkeypatch.setenv("GEMINI_API_KEY", secret)
    credentials = credentials_from_env({"gemini": "file-secret"})
    assert credentials.key_for("gemini") == secret
    assert secret not in repr(credentials)
    assert secret not in str(credentials)
    assert "file-secret" not in repr(credentials)

    handler = FakeHandler(['{"label": "ok"}'])
    messages = [Message(role="user", content=f"classify {secret}")]
    factory = LLMFactory(
        LLMSettings(default=RoleSpec(provider="fake", model="fake")),
        credentials,
        handlers={"fake": handler},
        sleep=lambda _: None,
    )
    with caplog.at_level(logging.INFO, logger="asgard.providers"):
        factory.generate("classifier", messages, schema=Classification)
    assert secret not in caplog.text
    assert "classify" not in caplog.text


def test_missing_key_does_not_echo_a_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    factory = LLMFactory(
        LLMSettings(default=RoleSpec(provider="gemini", model="gemini-2.5-flash")),
        Credentials(),
    )
    with pytest.raises(MissingAPIKey, match="missing API key for gemini") as caught:
        factory.generate("classifier", MESSAGES)
    assert "gemini-2.5-flash" not in str(caught.value)


def test_load_settings_lets_env_override_file_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "env-secret-value"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    path = tmp_path / "llm.toml"
    path.write_text(
        "\n".join(
            [
                "[default]",
                'provider = "openai"',
                'model = "gpt-4o-mini"',
                "",
                "[classifier]",
                'provider = "ollama"',
                'model = "llama3.1"',
                "",
                "[keys]",
                'openai = "file-secret-value"',
            ]
        ),
        encoding="utf-8",
    )
    settings, credentials = load_settings(path)
    assert settings.resolve("evaluator").model == "gpt-4o-mini"
    assert settings.resolve("classifier").provider == "ollama"
    assert credentials.key_for("openai") == secret
    assert secret not in repr(credentials)
    assert "file-secret-value" not in repr(credentials)


def test_providers_do_not_import_core_or_interfaces() -> None:
    forbidden = ("asgard.cli", "asgard.ui", "asgard.core")
    for path in PROVIDERS_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                assert not any(name == item or name.startswith(f"{item}.") for item in forbidden)
