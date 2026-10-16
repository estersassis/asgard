"""Run configuration: file, CLI overrides, redaction, and checks without the network."""

import io
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from asgard.cli.main import main
from asgard.core.config import (
    ConfigError,
    dump_toml,
    load_dotenv,
    load_run,
    prepare_run,
    probe_run,
)
from asgard.providers.handlers.fake import FakeHandler

TOKEN = "ghp_test_token"
KEY = "sk-test-key"
_OLLAMA_TAGS = json.dumps(
    {"models": [{"name": "llama3.1:latest"}, {"name": "llama-eval:latest"}]}
).encode()


class _Response:
    def __init__(
        self,
        headers: dict[str, str] | None = None,
        body: bytes = b"{}",
    ) -> None:
        self.headers = headers or {"X-OAuth-Scopes": "repo"}
        self._body = body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _http_error(url: str, code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "err", hdrs=None, fp=io.BytesIO(body))


def _ok(request: urllib.request.Request, timeout: int = 10) -> _Response:
    if "/api/tags" in request.full_url:
        return _Response(body=_OLLAMA_TAGS)
    return _Response()


def _settings(directory: Path) -> Path:
    target = directory / "asgard.toml"
    target.write_text(
        "\n".join(
            [
                'repo = "owner/name"',
                "max_iterations = 3",
                "prefilter = true",
                f'output_dir = "{directory}"',
                "",
                "[filters]",
                'state = "closed"',
                "merged_only = true",
                "",
                "[llm.default]",
                'provider = "ollama"',
                'model = "llama3.1"',
                "",
                "[keys]",
                f'openai = "{KEY}"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return target


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GITHUB_TOKEN", "GEMINI_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)


def test_flag_overrides_file_and_omitted_flag_stays(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    config_path = _settings(tmp_path)
    config, _ = load_run(config_path, {"max_iterations": 9, "filters": {"state": "open"}})
    assert config.max_iterations == 9
    assert config.batch_size == 1
    assert config.batch_max_chars is None
    assert config.mining.page_size == 50
    assert config.filters.state == "open"
    assert config.prefilter is True
    assert config.include_unresolved is False
    assert config.filters.merged_only is True
    assert config.llm.default.model == "llama3.1"
    from_url, _ = load_run(config_path, {"repo": "https://github.com/jpadilla/pyjwt.git"})
    assert from_url.repo == "jpadilla/pyjwt"


def test_mining_page_size_from_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    source = tmp_path / "asgard.toml"
    source.write_text(
        "\n".join(
            [
                'repo = "owner/name"',
                f'output_dir = "{tmp_path}"',
                "",
                "[mining]",
                "page_size = 25",
                "",
                "[llm.default]",
                'provider = "ollama"',
                'model = "llama3.1"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    config, _ = load_run(source)
    assert config.mining.page_size == 25
    assert config.redacted_dump()["mining"] == {"page_size": 25}


def test_prompts_selection_from_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from asgard.core.prompt_registry import PromptRegistry

    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    registry_dir = tmp_path / "prompt-store"
    saved = PromptRegistry(registry_dir).save(
        "classifier",
        name="strict",
        body="custom classifier\n",
    )
    source = tmp_path / "asgard.toml"
    source.write_text(
        "\n".join(
            [
                'repo = "owner/name"',
                f'output_dir = "{tmp_path / "out"}"',
                "",
                "[prompts]",
                f'classifier = "{saved.id}"',
                f'registry_dir = "{registry_dir}"',
                "",
                "[llm.default]",
                'provider = "fake"',
                'model = "fake"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    config, _ = load_run(source)
    assert config.prompts.classifier == saved.id
    assert config.prompts.evaluator == "default"
    assert config.prompts.synthesizer == "default"
    assert config.prompts.registry_dir == registry_dir

    prepared = prepare_run({}, source, dotenv=None, opener=_ok)
    assert prepared.prompts.classifier == saved.id
    dumped = prepared.redacted_dump()["prompts"]
    assert dumped["classifier"] == saved.id
    assert dumped["registry_dir"] == str(registry_dir)


def test_prepare_run_rejects_unknown_prompt_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    registry_dir = tmp_path / "empty-prompts"
    registry_dir.mkdir()
    source = tmp_path / "asgard.toml"
    source.write_text(
        "\n".join(
            [
                'repo = "owner/name"',
                f'output_dir = "{tmp_path / "out"}"',
                "",
                "[prompts]",
                'classifier = "does-not-exist"',
                f'registry_dir = "{registry_dir}"',
                "",
                "[llm.default]",
                'provider = "fake"',
                'model = "fake"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="unknown classifier prompt"):
        prepare_run({}, source, dotenv=None, opener=_ok)


def test_saved_config_reloads_without_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    source = tmp_path / "asgard.toml"
    source.write_text(
        "\n".join(
            [
                'repo = "owner/name"',
                f'github_token = "{TOKEN}-file"',
                f'output_dir = "{tmp_path / "run"}"',
                "",
                "[llm.default]",
                'provider = "ollama"',
                'model = "llama3.1"',
                "",
                "[llm.evaluator]",
                'model = "llama-eval"',
                "",
                "[keys]",
                f'gemini = "{KEY}"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    config = prepare_run({}, source, dotenv=None, opener=_ok)
    saved = (config.output_dir / "config.json").read_text(encoding="utf-8")
    assert TOKEN not in saved
    assert KEY not in saved
    assert "github_token" not in json.loads(saved)
    assert config.llm.evaluator is not None
    assert config.llm.evaluator.provider == "ollama"

    monkeypatch.setenv("GITHUB_TOKEN", "ghp_other")
    again, _ = load_run(config.output_dir / "config.json")
    assert again.redacted_dump() == config.redacted_dump()
    assert again.github_token.get_secret_value() == "ghp_other"


def test_cli_run_config_matches_saved_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setattr("asgard.core.config.load_dotenv", lambda path=Path(".env"): None)

    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        if request.full_url == "https://api.github.com/graphql":
            return _Response(
                body=json.dumps(
                    {
                        "data": {
                            "repository": {
                                "pullRequests": {
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                    "nodes": [],
                                }
                            },
                            "rateLimit": {
                                "cost": 1,
                                "remaining": 5000,
                                "resetAt": "2024-01-01T01:00:00Z",
                            },
                        }
                    }
                ).encode()
            )
        if "/api/tags" in request.full_url:
            return _Response(body=_OLLAMA_TAGS)
        return _Response()

    monkeypatch.setattr("asgard.core.config.urllib.request.urlopen", opener)
    source = _settings(tmp_path)
    out = tmp_path / "out"
    main(
        [
            "run",
            "other/repo",
            "--config",
            str(source),
            "--output-dir",
            str(out),
            "--max-iterations",
            "4",
            "--include-unresolved",
        ],
        opener=opener,
    )
    saved = json.loads((out / "config.json").read_text(encoding="utf-8"))
    assert saved["repo"] == "other/repo"
    assert saved["max_iterations"] == 4
    assert saved["filters"]["state"] == "closed"
    assert saved["prefilter"] is True
    assert saved["include_unresolved"] is True
    assert TOKEN not in json.dumps(saved)

    main(["run", "--config", str(out / "config.json")])
    reloaded = json.loads((out / "config.json").read_text(encoding="utf-8"))
    assert reloaded == saved


def test_rejects_bad_repo_and_dates_before_network(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    called = False

    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        nonlocal called
        called = True
        return _Response()

    with pytest.raises(ConfigError, match="owner/repo"):
        prepare_run({"repo": "nope"}, _settings(tmp_path), dotenv=None, opener=opener)
    with pytest.raises(ConfigError, match="since is after until"):
        load_run(_settings(tmp_path), {"filters": {"since": "2024-02-01", "until": "2024-01-01"}})
    with pytest.raises(ConfigError, match="max_iterations"):
        load_run(_settings(tmp_path), {"max_iterations": 0})
    with pytest.raises(ConfigError, match="batch_size"):
        load_run(_settings(tmp_path), {"batch_size": 0})
    with pytest.raises(ConfigError, match="batch_max_chars"):
        load_run(_settings(tmp_path), {"batch_max_chars": 0})
    with pytest.raises(ConfigError, match="page_size"):
        load_run(_settings(tmp_path), {"mining": {"page_size": 0}})
    assert called is False


def test_github_errors_are_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    path = _settings(tmp_path)

    def fail(code: int, scopes: str | None = "repo"):
        def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
            if "api.github.com" in request.full_url:
                raise _http_error(request.full_url, code, body=TOKEN.encode())
            return _Response({"X-OAuth-Scopes": scopes or ""})

        return opener

    with pytest.raises(ConfigError, match="invalid GitHub token") as invalid:
        prepare_run({}, path, dotenv=None, opener=fail(401))
    assert TOKEN not in str(invalid.value)
    with pytest.raises(ConfigError, match="repository not found"):
        prepare_run({}, path, dotenv=None, opener=fail(404))

    def limited(request: urllib.request.Request, timeout: int = 10) -> _Response:
        return _Response({"X-OAuth-Scopes": "read:org"})

    with pytest.raises(ConfigError, match="repo scope"):
        prepare_run({}, path, dotenv=None, opener=limited)


def test_provider_error_redacts_the_key(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    source = tmp_path / "asgard.toml"
    source.write_text(
        "\n".join(
            [
                'repo = "owner/name"',
                f'output_dir = "{tmp_path / "run"}"',
                "",
                "[llm.default]",
                'provider = "openai"',
                'model = "gpt-4o-mini"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        if "api.openai.com" in request.full_url:
            raise _http_error(request.full_url, 401, body=f"rejected {KEY}".encode())
        return _Response()

    with pytest.raises(ConfigError, match="openai is not reachable") as caught:
        prepare_run({}, source, dotenv=None, opener=opener)
    assert KEY not in str(caught.value)
    assert "***" in str(caught.value)


def test_missing_token_and_default_run_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "asgard.toml"
    source.write_text(
        "\n".join(
            [
                'repo = "owner/name"',
                "",
                "[llm.default]",
                'provider = "ollama"',
                'model = "llama3.1"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="missing GITHUB_TOKEN"):
        load_run(source)
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.chdir(tmp_path)
    config = prepare_run({}, source, dotenv=None, opener=_ok)
    assert config.output_dir.parts[-2] == "owner__name"
    assert "runs" in config.output_dir.parts
    assert (config.output_dir / "config.json").is_file()


def _approved_handler() -> FakeHandler:
    classification = json.dumps(
        {
            "has_security_discussion": False,
            "evidence": [],
            "rationale": "checked",
        }
    )
    findings = [
        {"criterion": name, "passed": True, "explanation": f"{name} checked"}
        for name in ("decision", "evidence", "completeness", "rationale")
    ]
    verdict = json.dumps({"verdict": "APPROVE", "findings": findings, "feedback": ""})
    return FakeHandler([classification, verdict])


def test_batch_flags_are_saved_and_copied_into_the_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setattr("asgard.core.config.load_dotenv", lambda path=Path(".env"): None)
    out = tmp_path / "out"
    source = tmp_path / "asgard.toml"
    source.write_text(
        "\n".join(
            [
                'repo = "jpadilla/pyjwt"',
                "batch_size = 2",
                f'output_dir = "{out}"',
                "",
                "[llm.default]",
                'provider = "fake"',
                'model = "fake"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        url = request.full_url
        if url.startswith("https://api.github.com/repos/") and "/pulls" not in url:
            return _Response()
        if url == "https://api.github.com/graphql":
            empty = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}
            return _Response(
                body=json.dumps(
                    {
                        "data": {
                            "repository": {
                                "pullRequests": {
                                    "pageInfo": {
                                        "hasNextPage": False,
                                        "endCursor": None,
                                    },
                                    "nodes": [
                                        {
                                            "id": "PR_node_4",
                                            "number": 4,
                                            "title": "change",
                                            "body": "change",
                                            "state": "OPEN",
                                            "merged": False,
                                            "createdAt": "2024-01-01T00:00:00Z",
                                            "updatedAt": "2024-01-01T00:00:00Z",
                                            "baseRepository": {
                                                "nameWithOwner": "jpadilla/pyjwt"
                                            },
                                            "comments": empty,
                                            "reviewThreads": empty,
                                        }
                                    ],
                                }
                            },
                            "rateLimit": {
                                "cost": 1,
                                "remaining": 5000,
                                "resetAt": "2024-01-01T01:00:00Z",
                            },
                        }
                    }
                ).encode()
            )
        return _Response(body=b"{}")

    main(
        [
            "run",
            "--config",
            str(source),
            "--max-prs",
            "1",
            "--quiet",
            "--batch-size",
            "4",
            "--batch-max-chars",
            "8000",
        ],
        opener=opener,
        handlers={"fake": _approved_handler()},
    )
    saved = json.loads((out / "config.json").read_text(encoding="utf-8"))
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert saved["batch_size"] == 4
    assert saved["batch_max_chars"] == 8000
    assert manifest["config"]["batch_size"] == 4
    assert manifest["config"]["batch_max_chars"] == 8000


def test_dotenv_does_not_override_the_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    env = tmp_path / ".env"
    env.write_text(f'GITHUB_TOKEN="from-file"\nOPENAI_API_KEY={KEY}\n', encoding="utf-8")
    load_dotenv(env)
    assert os.environ["GITHUB_TOKEN"] == TOKEN
    assert os.environ["OPENAI_API_KEY"] == KEY


def test_secrets_overlay_prefers_form_over_env_and_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "env-token")
    monkeypatch.setenv("OPENAI_API_KEY", "env-openai")
    source = tmp_path / "asgard.toml"
    source.write_text(
        "\n".join(
            [
                'repo = "owner/name"',
                f'github_token = "{TOKEN}-file"',
                f'output_dir = "{tmp_path / "run"}"',
                "",
                "[llm.default]",
                'provider = "openai"',
                'model = "gpt-4o-mini"',
                "",
                "[keys]",
                'openai = "file-openai"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    overlay = {
        "GITHUB_TOKEN": "overlay-token",
        "OPENAI_API_KEY": "overlay-openai",
    }
    config, credentials = load_run(source, secrets=overlay)
    assert config.github_token.get_secret_value() == "overlay-token"
    assert credentials.key_for("openai") == "overlay-openai"
    assert os.environ["GITHUB_TOKEN"] == "env-token"
    assert os.environ["OPENAI_API_KEY"] == "env-openai"

    prepare_run({}, source, dotenv=None, opener=_ok, secrets=overlay)
    saved = (tmp_path / "run" / "config.json").read_text(encoding="utf-8")
    assert "overlay-token" not in saved
    assert "overlay-openai" not in saved
    assert "github_token" not in json.loads(saved)
    # prepare_run keeps overlay env for the process (pipeline / provider factory).
    assert os.environ["GITHUB_TOKEN"] == "overlay-token"
    assert os.environ["OPENAI_API_KEY"] == "overlay-openai"


def test_overrides_peel_github_token_and_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "asgard.toml"
    source.write_text(
        "\n".join(
            [
                'repo = "owner/name"',
                f'output_dir = "{tmp_path}"',
                "",
                "[llm.default]",
                'provider = "openai"',
                'model = "gpt-4o-mini"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    config, credentials = load_run(
        source,
        {
            "github_token": TOKEN,
            "keys": {"openai": KEY},
            "max_iterations": 5,
        },
    )
    assert config.github_token.get_secret_value() == TOKEN
    assert config.max_iterations == 5
    assert credentials.key_for("openai") == KEY
    assert "github_token" not in config.redacted_dump()


def test_probe_run_collects_github_and_provider_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    source = tmp_path / "asgard.toml"
    source.write_text(
        "\n".join(
            [
                'repo = "owner/name"',
                f'output_dir = "{tmp_path}"',
                "",
                "[llm.default]",
                'provider = "openai"',
                'model = "gpt-4o-mini"',
                "",
                "[llm.evaluator]",
                'provider = "ollama"',
                'model = "llama3.1"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    config, credentials = load_run(source)

    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        url = request.full_url
        if "api.github.com" in url:
            raise _http_error(url, 404)
        if "api.openai.com" in url:
            raise _http_error(url, 401, body=f"bad {KEY}".encode())
        if "/api/tags" in url:
            return _Response(body=_OLLAMA_TAGS)
        return _Response()

    results = probe_run(config, credentials, opener=opener)
    assert results["github"]["ok"] is False
    assert "repository not found" in results["github"]["message"]
    assert results["openai"]["ok"] is False
    assert KEY not in results["openai"]["message"]
    assert results["ollama"]["ok"] is True
    assert results["ollama"]["message"] == ""


def test_ollama_requires_pulled_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    path = _settings(tmp_path)

    def missing(request: urllib.request.Request, timeout: int = 10) -> _Response:
        if "/api/tags" in request.full_url:
            return _Response(body=json.dumps({"models": [{"name": "other:latest"}]}).encode())
        return _Response()

    with pytest.raises(ConfigError, match="ollama model is not pulled"):
        prepare_run({}, path, dotenv=None, opener=missing)


def test_dump_toml_roundtrips_without_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    data = {
        "repo": "owner/name",
        "max_iterations": 3,
        "batch_size": 1,
        "prefilter": True,
        "github_token": TOKEN,
        "keys": {"openai": KEY},
        "filters": {"state": "closed", "max_prs": 10, "since": "2024-01-01"},
        "mining": {"page_size": 25},
        "prompts": {
            "classifier": "strict",
            "evaluator": "default",
            "synthesizer": "default",
            "registry_dir": str(tmp_path / "prompts"),
        },
        "llm": {
            "default": {
                "provider": "ollama",
                "model": "llama3.1",
                "temperature": 0,
                "timeout": 300,
            },
            "classifier": {"provider": "ollama", "model": "llama3.1"},
            "evaluator": {"provider": "openai", "model": "gpt-4o-mini"},
            "synthesizer": {"provider": "default"},
        },
    }
    text = dump_toml(data)
    assert TOKEN not in text
    assert KEY not in text
    assert "github_token" not in text
    assert "[keys]" not in text
    assert "output_dir" not in text
    assert "[llm.classifier]" not in text
    assert "[llm.synthesizer]" not in text
    assert "[llm.evaluator]" in text
    assert 'provider = "openai"' in text
    assert "[prompts]" in text
    assert 'classifier = "strict"' in text
    assert "evaluator" not in text.split("[prompts]", 1)[1].split("[", 1)[0]

    target = tmp_path / "asgard.toml"
    target.write_text(text, encoding="utf-8")
    config, _ = load_run(target, {"output_dir": str(tmp_path / "out")})
    assert config.repo == "owner/name"
    assert config.filters.state == "closed"
    assert config.filters.max_prs == 10
    assert config.mining.page_size == 25
    assert config.prefilter is True
    assert config.prompts.classifier == "strict"
    assert config.prompts.evaluator == "default"
    assert config.prompts.registry_dir == tmp_path / "prompts"
    assert config.llm.default.provider == "ollama"
    assert config.llm.evaluator is not None
    assert config.llm.evaluator.provider == "openai"
    assert config.llm.classifier is None or config.llm.classifier.model == "llama3.1"
