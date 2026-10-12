"""Run configuration: file, CLI overrides, redaction, and checks without the network."""

import io
import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from asgard.cli.main import main
from asgard.core.config import ConfigError, load_dotenv, load_run, prepare_run

TOKEN = "ghp_test_token"
KEY = "sk-test-key"


class _Response:
    def __init__(self, headers: dict[str, str] | None = None) -> None:
        self.headers = headers or {"X-OAuth-Scopes": "repo"}

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return b"{}"


def _http_error(url: str, code: int, body: bytes = b"") -> urllib.error.HTTPError:
    return urllib.error.HTTPError(url, code, "err", hdrs=None, fp=io.BytesIO(body))


def _ok(request: urllib.request.Request, timeout: int = 10) -> _Response:
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
    assert config.filters.state == "open"
    assert config.prefilter is True
    assert config.filters.merged_only is True
    assert config.llm.default.model == "llama3.1"
    from_url, _ = load_run(config_path, {"repo": "https://github.com/jpadilla/pyjwt.git"})
    assert from_url.repo == "jpadilla/pyjwt"


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
    monkeypatch.setattr("asgard.core.config.urllib.request.urlopen", _ok)
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
        ]
    )
    saved = json.loads((out / "config.json").read_text(encoding="utf-8"))
    assert saved["repo"] == "other/repo"
    assert saved["max_iterations"] == 4
    assert saved["filters"]["state"] == "closed"
    assert saved["prefilter"] is True
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
