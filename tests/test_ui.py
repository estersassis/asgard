"""Web UI: start a run through the UI layer with the fake provider. No network."""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from asgard.core.config import load_run
from asgard.core.events import ClassificationAttempt, Error
from asgard.core.orchestrator import Orchestrator
from asgard.core.stage import CancelledError, RunContext, Stage
from asgard.providers.handlers.fake import FakeHandler
from asgard.ui.app import create_app
from asgard.ui.sink import UISink

TOKEN = "ghp_secret"
KEY = "sk-ui-test-key"
FIXTURES = Path(__file__).parent / "fixtures" / "pyjwt"
_API = "https://api.github.com/repos/jpadilla/pyjwt"
_GRAPHQL = "https://api.github.com/graphql"
_QUOTE = "the allow-list is what prevents algorithm-confusion"
_TOKEN_HEX = re.compile(
    r"#[0-9A-Fa-f]{3}(?:[0-9A-Fa-f]{3})?\b"
)


class _Response:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = headers or {"X-OAuth-Scopes": "public_repo"}

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _empty_connection() -> dict[str, object]:
    return {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}


def _graphql_state(rest: dict[str, object]) -> str:
    if rest.get("merged") is True:
        return "MERGED"
    state = rest.get("state")
    if state == "open":
        return "OPEN"
    if state == "closed":
        return "CLOSED"
    return "OPEN"


def _numbers() -> list[int]:
    listed = json.loads((FIXTURES / "pulls.json").read_text(encoding="utf-8"))
    return [item["number"] for item in listed]


def _node_from_fixture(number: int) -> dict[str, object]:
    rest = json.loads((FIXTURES / f"{number}.json").read_text(encoding="utf-8"))
    listed = next(
        item
        for item in json.loads((FIXTURES / "pulls.json").read_text(encoding="utf-8"))
        if item["number"] == number
    )
    return {
        "id": rest.get("node_id") or f"PR_node_{number}",
        "number": number,
        "title": rest.get("title") or listed.get("title") or "",
        "body": rest.get("body") or "",
        "state": _graphql_state(rest),
        "merged": rest.get("merged") is True,
        "createdAt": rest.get("created_at") or listed.get("created_at"),
        "updatedAt": rest.get("updated_at") or listed.get("updated_at"),
        "baseRepository": {"nameWithOwner": "jpadilla/pyjwt"},
        "comments": _empty_connection(),
        "reviewThreads": _empty_connection(),
    }


def _graphql_page(numbers: list[int]) -> bytes:
    return json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequests": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [_node_from_fixture(number) for number in numbers],
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


def _opener(numbers: list[int]):
    repo_body = (FIXTURES / "repo.json").read_bytes()

    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        url = request.full_url
        if url not in (_API, _GRAPHQL):
            raise AssertionError(f"unexpected URL: {url}")
        if url == _API:
            return _Response(repo_body)
        return _Response(_graphql_page(numbers))

    return opener


def _findings() -> list[dict[str, object]]:
    return [
        {"criterion": name, "passed": True, "explanation": f"{name} checked"}
        for name in ("decision", "evidence", "completeness", "rationale")
    ]


def _handler(security: int) -> FakeHandler:
    approved = json.dumps(
        {
            "has_security_discussion": True,
            "evidence": [{"quote": _QUOTE, "location": "description"}],
            "rationale": "the description discusses authentication",
        }
    )
    rejected = json.dumps(
        {
            "has_security_discussion": False,
            "evidence": [],
            "rationale": "documentation only",
        }
    )
    verdict = json.dumps({"verdict": "APPROVE", "findings": _findings(), "feedback": ""})
    synthesis = json.dumps(
        {
            "topics": [
                {
                    "title": "Authentication",
                    "recommendations": [
                        {
                            "title": "Keep the algorithm allow-list a sequence",
                            "text": "Reject a bare string so the allow-list cannot be bypassed.",
                            "pr_ids": [str(security)],
                        }
                    ],
                }
            ]
        }
    )
    return FakeHandler([approved, verdict, rejected, verdict, synthesis])


def _settings(directory: Path, out: Path) -> Path:
    target = directory / "asgard.toml"
    target.write_text(
        "\n".join(
            [
                'repo = "jpadilla/pyjwt"',
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
    return target


def _wait_done(client: TestClient, run_id: str, *, timeout: float = 30) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = client.get(f"/api/runs/{run_id}").json()
        if not status["running"]:
            return status
        time.sleep(0.05)
    raise AssertionError("run did not finish")


def _events(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        event.pop("at", None)
        rows.append(event)
    return rows


def test_ui_run_matches_cli_run_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    numbers = _numbers()
    security = numbers[0]
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    out = tmp_path / "out"
    source = _settings(tmp_path, out)
    app = create_app(
        opener=_opener(numbers),
        handlers={"fake": _handler(security)},
        dotenv=None,
    )
    client = TestClient(app)

    response = client.post("/api/runs", json={"config": str(source)})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert Path(payload["output_dir"]) == out
    status = _wait_done(client, payload["id"])
    assert status["error"] is None

    assert (out / "config.json").is_file()
    assert (out / "events.jsonl").is_file()
    assert (out / "SECURITY.md").is_file()
    assert (out / "manifest.json").is_file()
    for stage in (
        "mine",
        "normalize",
        "prefilter",
        "classify",
        "consolidate",
        "synthesize",
        "security_md",
        "bundle",
    ):
        assert (out / stage / "output.json").is_file()

    events = _events(out / "events.jsonl")
    assert {"type": "StageStarted", "stage": "mine", "total": None} in events
    assert any(event["type"] == "ClassifyProgress" for event in events)
    assert events[-1] == {"type": "StageFinished", "stage": "bundle", "skipped": False}

    for path in out.rglob("*"):
        if path.is_file():
            assert TOKEN not in path.read_text(encoding="utf-8")


def test_uisink_forwards_classify_progress() -> None:
    sink = UISink()
    from asgard.core.events import ClassifyProgress

    sink.handle(ClassifyProgress(kind="pull", index=1, total=2))
    sink.close()
    event = sink.get(timeout=1)
    assert event is not None
    assert event["type"] == "ClassifyProgress"
    assert event["kind"] == "pull"
    assert event["index"] == 1
    assert event["total"] == 2
    assert "at" in event
    assert sink.get(timeout=1) is None


def test_shell_and_themes() -> None:
    app = create_app(dotenv=None)
    client = TestClient(app)

    dark = client.get("/setup")
    assert dark.status_code == 200
    assert "ASGARD" in dark.text
    assert "I</span>Setup" in dark.text
    assert "II</span>Pipeline" in dark.text
    assert "III</span>Council" in dark.text
    assert "IV</span>Guideline" in dark.text
    assert "V</span>Forge" in dark.text
    assert 'data-theme="light"' not in dark.text
    assert "var(--ember)" in dark.text
    assert "Open the gate to" in dark.text
    assert 'aria-label="Run setup"' in dark.text
    assert "Start run" in dark.text
    assert "+ More options" in dark.text
    assert "Load asgard.toml" in dark.text
    assert "Save asgard.toml" in dark.text
    assert "/static/css/setup.css" in dark.text
    assert "/static/js/setup.js" in dark.text

    home = client.get("/")
    assert home.status_code == 200
    assert 'aria-label="Run setup"' in home.text

    light = client.get("/setup?theme=light")
    assert light.status_code == 200
    assert 'data-theme="light"' in light.text

    tokens = client.get("/static/css/tokens.css")
    assert tokens.status_code == 200
    assert "--stone:" in tokens.text
    assert '--ember: #D68C42' in tokens.text or "--ember:#D68C42" in tokens.text.replace(" ", "")
    assert 'data-theme="light"' in tokens.text

    setup_css = client.get("/static/css/setup.css")
    assert setup_css.status_code == 200
    assert ".inp" in setup_css.text
    assert ".hint" in setup_css.text

    setup_js = client.get("/static/js/setup.js")
    assert setup_js.status_code == 200
    assert "/api/setup/validate" in setup_js.text
    assert "/api/runs" in setup_js.text
    assert "/pipeline?run=" in setup_js.text

    templates = Path(__file__).resolve().parents[1] / "asgard" / "ui" / "templates"
    for path in templates.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        # Hex colors belong in tokens.css only.
        assert not _TOKEN_HEX.search(text), path


def test_pipeline_page_assets() -> None:
    app = create_app(dotenv=None)
    client = TestClient(app)

    page = client.get("/pipeline")
    assert page.status_code == 200
    assert "ASGARD" in page.text
    assert "II</span>Pipeline" in page.text
    assert "/static/css/pipeline.css" in page.text
    assert "/static/js/pipeline.js" in page.text
    assert 'data-theme="light"' not in page.text
    assert not _TOKEN_HEX.search(page.text)

    light = client.get("/pipeline?theme=light")
    assert light.status_code == 200
    assert 'data-theme="light"' in light.text

    css = client.get("/static/css/pipeline.css")
    assert css.status_code == 200
    assert ".stage" in css.text
    assert "var(--ember)" in css.text or "var(--bifrost)" in css.text

    js = client.get("/static/js/pipeline.js")
    assert js.status_code == 200
    assert "/api/runs/" in js.text
    assert "/events" in js.text
    assert "/log" in js.text
    assert "/council?run=" in js.text


def test_pipeline_log_config_and_removed_endpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    numbers = _numbers()
    security = numbers[0]
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    out = tmp_path / "out"
    source = _settings(tmp_path, out)
    app = create_app(
        opener=_opener(numbers),
        handlers={"fake": _handler(security)},
        dotenv=None,
    )
    client = TestClient(app)

    missing = client.get("/api/runs/missing/log")
    assert missing.status_code == 404

    response = client.post("/api/runs", json={"config": str(source)})
    assert response.status_code == 200, response.text
    run_id = response.json()["id"]
    status = _wait_done(client, run_id)
    assert status["error"] is None

    disk_log = (out / "events.jsonl").read_text(encoding="utf-8")
    api_log = client.get(f"/api/runs/{run_id}/log")
    assert api_log.status_code == 200
    assert "text/plain" in api_log.headers.get("content-type", "")
    assert api_log.text == disk_log
    assert "StageStarted" in api_log.text
    assert "ClassifyProgress" in api_log.text
    disk_events = _events(out / "events.jsonl")
    api_events = [
        {key: value for key, value in json.loads(line).items() if key != "at"}
        for line in api_log.text.splitlines()
        if line
    ]
    assert api_events == disk_events
    assert {"type": "StageStarted", "stage": "mine", "total": None} in api_events
    assert any(event["type"] == "ClassifyProgress" for event in api_events)

    disk_config = json.loads((out / "config.json").read_text(encoding="utf-8"))
    api_config = client.get(f"/api/runs/{run_id}/config")
    assert api_config.status_code == 200
    assert api_config.json() == disk_config
    assert api_config.json()["repo"] == "jpadilla/pyjwt"
    assert "prefilter" in api_config.json()

    removed_path = out / "prefilter" / "removed.json"
    assert removed_path.is_file()
    disk_removed = json.loads(removed_path.read_text(encoding="utf-8"))
    api_removed = client.get(f"/api/runs/{run_id}/prefilter/removed")
    assert api_removed.status_code == 200
    assert api_removed.json() == disk_removed
    assert {"before", "after", "removed"} <= set(api_removed.json())


def test_stop_and_resume_from_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    numbers = _numbers()
    security = numbers[0]
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    out = tmp_path / "out"
    source = _settings(tmp_path, out)
    gate = threading.Event()
    released = threading.Event()

    class GatedHandler(FakeHandler):
        def generate(self, messages, **kwargs):  # type: ignore[no-untyped-def]
            released.set()
            gate.wait(timeout=5)
            return super().generate(messages, **kwargs)

    handler = GatedHandler(
        [
            json.dumps(
                {
                    "has_security_discussion": True,
                    "evidence": [{"quote": _QUOTE, "location": "description"}],
                    "rationale": "auth",
                }
            ),
            json.dumps({"verdict": "APPROVE", "findings": _findings(), "feedback": ""}),
            json.dumps(
                {
                    "has_security_discussion": False,
                    "evidence": [],
                    "rationale": "docs",
                }
            ),
            json.dumps({"verdict": "APPROVE", "findings": _findings(), "feedback": ""}),
            json.dumps(
                {
                    "topics": [
                        {
                            "title": "Authentication",
                            "recommendations": [
                                {
                                    "title": "Allow-list",
                                    "text": "Keep it a sequence.",
                                    "pr_ids": [str(security)],
                                }
                            ],
                        }
                    ]
                }
            ),
        ]
    )
    app = create_app(
        opener=_opener(numbers),
        handlers={"fake": handler},
        dotenv=None,
    )
    client = TestClient(app)
    started = client.post("/api/runs", json={"config": str(source)})
    assert started.status_code == 200
    run_id = started.json()["id"]
    assert released.wait(timeout=5)

    stop = client.post(f"/api/runs/{run_id}/stop")
    assert stop.status_code == 200
    gate.set()
    status = _wait_done(client, run_id)
    assert status["error"] == "run stopped"
    assert not (out / "bundle" / "output.json").exists()

    mine_output = out / "mine" / "output.json"
    assert mine_output.is_file()
    mine_before = mine_output.read_text(encoding="utf-8")
    mine_mtime = mine_output.stat().st_mtime_ns

    # Finish the interrupted classify item work that may have completed, then resume.
    if (out / "classify" / "output.json").exists():
        from_stage = "consolidate"
    elif (out / "prefilter" / "output.json").exists():
        from_stage = "classify"
    else:
        from_stage = "mine"

    # Provide a fresh handler script for the remainder of the pipeline.
    store = app.state.store
    session = store.get(run_id)
    assert session is not None
    session.handlers = {"fake": _handler(security)}

    resumed = client.post(f"/api/runs/{run_id}/resume", json={"from": from_stage})
    assert resumed.status_code == 200, resumed.text
    status = _wait_done(client, run_id)
    assert status["error"] is None
    assert (out / "SECURITY.md").is_file()
    assert (out / "manifest.json").is_file()
    # Resume from a later stage must not re-mine; PR list and file stay untouched.
    assert from_stage != "mine"
    assert mine_output.read_text(encoding="utf-8") == mine_before
    assert mine_output.stat().st_mtime_ns == mine_mtime


def test_setup_env_prefills_from_dotenv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text(
        f"GITHUB_TOKEN={TOKEN}\nOPENAI_API_KEY={KEY}\n",
        encoding="utf-8",
    )
    app = create_app(dotenv=env)
    client = TestClient(app)
    response = client.get("/api/setup/env")
    assert response.status_code == 200
    payload = response.json()
    assert payload["GITHUB_TOKEN"] == TOKEN
    assert payload["OPENAI_API_KEY"] == KEY
    assert payload["OLLAMA_HOST"] == "http://localhost:11434"


def test_setup_toml_and_parse_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    app = create_app(dotenv=None)
    client = TestClient(app)
    overrides = {
        "repo": "owner/name",
        "max_iterations": 4,
        "batch_size": 10,
        "prefilter": True,
        "filters": {"state": "closed", "max_prs": 20, "since": "2023-01-01"},
        "llm": {
            "default": {"provider": "ollama", "model": "llama3.1"},
            "evaluator": {"provider": "openai", "model": "gpt-4o-mini"},
        },
    }
    saved = client.post("/api/setup/toml", json={"overrides": overrides})
    assert saved.status_code == 200
    text = saved.text
    assert TOKEN not in text
    assert KEY not in text
    assert "github_token" not in text
    assert "[keys]" not in text
    assert "sk-" not in text
    assert 'repo = "owner/name"' in text
    assert "[llm.default]" in text
    assert "[llm.evaluator]" in text

    rejected = client.post(
        "/api/setup/toml",
        json={"overrides": {**overrides, "github_token": TOKEN}},
    )
    assert rejected.status_code == 400

    parsed = client.post("/api/setup/parse", json={"text": text})
    assert parsed.status_code == 200
    fields = parsed.json()
    assert "github_token" not in fields
    assert "keys" not in fields
    assert fields["repo"] == "owner/name"
    assert fields["batch_size"] == 10
    assert fields["prefilter"] is True
    assert fields["llm"]["evaluator"]["provider"] == "openai"

    with_secrets = client.post(
        "/api/setup/parse",
        json={
            "text": text
            + '\ngithub_token = "ghp_should_strip"\n[keys]\nopenai = "sk-strip"\n'
        },
    )
    assert with_secrets.status_code == 200
    stripped = with_secrets.json()
    assert "github_token" not in stripped
    assert "keys" not in stripped

    target = tmp_path / "asgard.toml"
    target.write_text(text, encoding="utf-8")
    config, _ = load_run(target, {"output_dir": str(tmp_path / "out")})
    assert config.repo == "owner/name"
    assert config.max_iterations == 4
    assert config.batch_size == 10
    assert config.prefilter is True
    assert config.filters.state == "closed"
    assert config.llm.evaluator.provider == "openai"


def test_setup_validate_probes_resolved_providers_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    seen: list[str] = []

    def opener(request: urllib.request.Request, timeout: int = 10) -> Any:
        url = request.full_url
        seen.append(url)
        if "api.github.com" in url:
            return _Response(b"{}")
        if "/api/tags" in url:
            return _Response(
                json.dumps({"models": [{"name": "llama3.1:latest"}]}).encode()
            )
        if "api.openai.com" in url:
            return _Response(b"{}")
        raise AssertionError(f"unexpected URL: {url}")

    app = create_app(opener=opener, dotenv=None)
    client = TestClient(app)

    ollama_only = client.post(
        "/api/setup/validate",
        json={
            "overrides": {
                "repo": "jpadilla/pyjwt",
                "output_dir": str(tmp_path / "a"),
                "llm": {"default": {"provider": "ollama", "model": "llama3.1"}},
            },
            "secrets": {"GITHUB_TOKEN": TOKEN},
        },
    )
    assert ollama_only.status_code == 200, ollama_only.text
    body = ollama_only.json()
    assert body["github"]["ok"] is True
    assert body["ollama"]["ok"] is True
    assert "openai" not in body
    assert any("/api/tags" in url for url in seen)
    assert not any("api.openai.com" in url for url in seen)

    seen.clear()
    mixed = client.post(
        "/api/setup/validate",
        json={
            "overrides": {
                "repo": "jpadilla/pyjwt",
                "output_dir": str(tmp_path / "b"),
                "llm": {
                    "default": {"provider": "ollama", "model": "llama3.1"},
                    "evaluator": {"provider": "openai", "model": "gpt-4o-mini"},
                },
            },
            "secrets": {"GITHUB_TOKEN": TOKEN, "OPENAI_API_KEY": KEY},
        },
    )
    assert mixed.status_code == 200, mixed.text
    body = mixed.json()
    assert body["ollama"]["ok"] is True
    assert body["openai"]["ok"] is True
    assert any("api.openai.com" in url for url in seen)


def test_start_run_with_secrets_omits_them_from_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    numbers = _numbers()
    security = numbers[0]
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    out = tmp_path / "out"
    source = _settings(tmp_path, out)
    app = create_app(
        opener=_opener(numbers),
        handlers={"fake": _handler(security)},
        dotenv=None,
    )
    client = TestClient(app)
    response = client.post(
        "/api/runs",
        json={
            "config": str(source),
            "secrets": {"GITHUB_TOKEN": TOKEN},
        },
    )
    assert response.status_code == 200, response.text
    status = _wait_done(client, response.json()["id"])
    assert status["error"] is None
    saved = (out / "config.json").read_text(encoding="utf-8")
    assert TOKEN not in saved
    assert "github_token" not in json.loads(saved)


def test_start_run_from_setup_overrides_omits_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UI Start path: overrides + secrets overlay, same redacted config.json as CLI."""
    numbers = _numbers()
    security = numbers[0]
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    out = tmp_path / "out"
    app = create_app(
        opener=_opener(numbers),
        handlers={"fake": _handler(security)},
        dotenv=None,
    )
    client = TestClient(app)
    response = client.post(
        "/api/runs",
        json={
            "overrides": {
                "repo": "jpadilla/pyjwt",
                "output_dir": str(out),
                "llm": {"default": {"provider": "fake", "model": "fake"}},
            },
            "secrets": {"GITHUB_TOKEN": TOKEN, "OPENAI_API_KEY": KEY},
        },
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "id" in payload
    status = _wait_done(client, payload["id"])
    assert status["error"] is None
    saved = json.loads((out / "config.json").read_text(encoding="utf-8"))
    assert "github_token" not in saved
    assert "keys" not in saved
    dumped = json.dumps(saved)
    assert TOKEN not in dumped
    assert KEY not in dumped


def test_cancel_stops_between_items(tmp_path: Path) -> None:
    seen: list[str] = []
    cancel_after = threading.Event()

    class Slow(Stage):
        name = "classify"

        def execute(self, ctx: RunContext, data: Any) -> Any:
            def work(item_id: str, item: str) -> str:
                seen.append(item_id)
                ctx.emit(ClassificationAttempt(pr_id=item_id))
                if item_id == "1":
                    cancel_after.set()
                    time.sleep(0.2)
                return item

            return self.process_items(ctx, [(pr, pr) for pr in data], work)

    class ListSink:
        def __init__(self) -> None:
            self.events: list[Any] = []

        def handle(self, event: Any) -> None:
            self.events.append(event)

    flag = threading.Event()

    def cancel() -> bool:
        return flag.is_set()

    sink = ListSink()

    def run() -> None:
        with pytest.raises(CancelledError, match="run stopped"):
            Orchestrator([Slow()], [sink]).run(
                tmp_path,
                data=["1", "2", "3"],
                cancel=cancel,
            )

    thread = threading.Thread(target=run)
    thread.start()
    assert cancel_after.wait(timeout=5)
    flag.set()
    thread.join(timeout=5)
    assert seen[0] == "1"
    assert "3" not in seen
    assert any(isinstance(event, Error) and event.message == "run stopped" for event in sink.events)
