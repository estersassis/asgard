"""Web UI: start a run through the UI layer with the fake provider. No network."""

from __future__ import annotations

import io
import json
import re
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from asgard.core.config import load_run
from asgard.core.events import ClassificationAttempt, Error
from asgard.core.orchestrator import Orchestrator
from asgard.core.pilot.set import load_pilot_set
from asgard.core.prompt_registry import PromptRegistry, content_sha256, resolve_system_prompt
from asgard.core.stage import CancelledError, RunContext, Stage
from asgard.prompts.synthesizer import SYSTEM_PROMPT
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
    assert 'id="runs-footer"' in dark.text
    assert "/static/js/shell.js" in dark.text
    assert "Past runs" in dark.text

    home = client.get("/")
    assert home.status_code == 200
    assert 'aria-label="Run setup"' in home.text
    assert 'id="runs-footer"' in home.text

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
    assert "/api/setup/models" in setup_js.text
    assert "/pipeline?run=" in setup_js.text

    shell_js = client.get("/static/js/shell.js")
    assert shell_js.status_code == 200
    assert "/api/runs" in shell_js.text
    assert "runs-footer" in shell_js.text or "runs-menu-list" in shell_js.text

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
    assert "startedAt" in js.text
    assert "formatElapsed" in js.text


def test_nav_preserves_run_query() -> None:
    app = create_app(dotenv=None)
    client = TestClient(app)
    run_id = "6168dcdaf88e4bfb99a1e6b5d35f3cc5"
    page = client.get(f"/pipeline?run={run_id}")
    assert page.status_code == 200
    for path in ("/setup", "/pipeline", "/council", "/guideline", "/forge"):
        assert f"{path}?run={run_id}" in page.text

    bare = client.get("/pipeline")
    assert bare.status_code == 200
    assert "/council" in bare.text
    assert "/council?run=" not in bare.text


def test_council_page_assets() -> None:
    app = create_app(dotenv=None)
    client = TestClient(app)

    page = client.get("/council")
    assert page.status_code == 200
    assert "ASGARD" in page.text
    assert "III</span>Council" in page.text
    assert "The <em>council</em>" in page.text
    assert "/static/css/council.css" in page.text
    assert "/static/js/council.js" in page.text
    assert 'id="council"' in page.text
    assert 'data-run="' in page.text
    assert 'id="council-security"' in page.text
    assert 'id="council-not-security"' in page.text
    assert 'id="council-unresolved"' in page.text
    assert 'id="council-error-total"' in page.text
    assert 'id="council-retry"' in page.text
    assert 'id="council-retry-all"' in page.text
    assert 'id="council-loop"' in page.text
    assert 'aria-label="Pull requests"' in page.text
    assert 'aria-label="Status filter"' in page.text
    assert 'id="council-pr-list"' in page.text
    assert 'aria-label="Deliberation"' in page.text
    assert 'data-theme="light"' not in page.text
    assert not _TOKEN_HEX.search(page.text)

    with_run = client.get("/council?run=abc123")
    assert with_run.status_code == 200
    assert 'data-run="abc123"' in with_run.text

    light = client.get("/council?theme=light")
    assert light.status_code == 200
    assert 'data-theme="light"' in light.text

    css = client.get("/static/css/council.css")
    assert css.status_code == 200
    assert "1080px" in css.text
    assert ".pr" in css.text
    assert ".council-filter" in css.text
    assert ".dot" in css.text
    assert ".dot.live" in css.text
    assert "@keyframes pulse" in css.text
    assert "var(--ember-hi)" in css.text
    assert "var(--bifrost)" in css.text

    js = client.get("/static/js/council.js")
    assert js.status_code == 200
    assert "EventSource" in js.text
    assert "/events" in js.text
    assert "ClassificationAttempt" in js.text
    assert "EvaluatorVerdict" in js.text
    assert "LoopFinished" in js.text
    assert "STATUS_FILTERS" in js.text
    assert "statusFilter" in js.text
    assert "/classify/trace" in js.text
    assert "/classify/metrics" in js.text
    assert "/classify/output" in js.text
    assert "/classify/items" in js.text
    assert "/classify/retry" in js.text
    assert "/prefilter/output" in js.text
    assert "/mine/index" in js.text
    assert '"why"' in js.text or "why" in js.text
    assert '"hide"' in js.text or "hide" in js.text


def test_guideline_page_assets() -> None:
    app = create_app(dotenv=None)
    client = TestClient(app)

    page = client.get("/guideline")
    assert page.status_code == 200
    assert "ASGARD" in page.text
    assert "IV</span>Guideline" in page.text
    assert "/static/css/guideline.css" in page.text
    assert "/static/js/guideline.js" in page.text
    assert 'id="guideline"' in page.text
    assert 'data-run="' in page.text
    assert 'id="guideline-label"' in page.text
    assert 'id="guideline-edit"' in page.text
    assert 'id="guideline-bundle"' in page.text
    assert 'id="guideline-download"' in page.text
    assert 'id="guideline-prompt"' in page.text
    assert 'id="guideline-regenerate"' in page.text
    assert 'id="guideline-preview"' in page.text
    assert 'class="ms"' in page.text
    assert "SECURITY.md" in page.text
    assert "Edit prompt" in page.text
    assert "Regenerate" in page.text
    assert 'data-theme="light"' not in page.text
    assert not _TOKEN_HEX.search(page.text)

    with_run = client.get("/guideline?run=abc123")
    assert with_run.status_code == 200
    assert 'data-run="abc123"' in with_run.text

    light = client.get("/guideline?theme=light")
    assert light.status_code == 200
    assert 'data-theme="light"' in light.text

    css = client.get("/static/css/guideline.css")
    assert css.status_code == 200
    assert "760px" in css.text
    assert ".ms" in css.text
    assert ".cite" in css.text
    assert "var(--ember)" in css.text or "var(--panel)" in css.text

    js = client.get("/static/js/guideline.js")
    assert js.status_code == 200
    assert "/SECURITY.md" in js.text
    assert "/consolidate/output" in js.text
    assert "/security_md/output" in js.text
    assert "/synthesize/prompt" in js.text
    assert "/regenerate" in js.text
    assert "/bundle.zip" in js.text
    assert "unknown_citations" in js.text


def test_forge_page_assets() -> None:
    app = create_app(dotenv=None)
    client = TestClient(app)

    page = client.get("/forge")
    assert page.status_code == 200
    assert "ASGARD" in page.text
    assert "V</span>Forge" in page.text
    assert "The <em>forge</em>" in page.text
    assert "/static/css/forge.css" in page.text
    assert "/static/js/forge.js" in page.text
    assert 'id="forge"' in page.text
    assert 'id="forge-tabs"' in page.text
    assert 'id="forge-tab-classifier"' in page.text
    assert 'id="forge-version"' in page.text
    assert 'id="forge-save"' in page.text
    assert 'id="forge-save-form"' in page.text
    assert 'id="forge-save-name"' in page.text
    assert 'id="forge-prompt"' in page.text
    assert 'id="forge-pilot-set"' in page.text
    assert 'id="forge-pilot-mode"' in page.text
    assert 'id="forge-batch-size"' in page.text
    assert "Classifier only" in page.text
    assert "Classifier + evaluator" in page.text
    assert 'id="forge-run"' in page.text
    assert 'id="forge-promote"' in page.text
    assert 'id="forge-reject"' in page.text
    assert 'id="forge-config"' in page.text
    assert 'disabled' in page.text
    assert 'data-theme="light"' not in page.text
    assert not _TOKEN_HEX.search(page.text)

    light = client.get("/forge?theme=light")
    assert light.status_code == 200
    assert 'data-theme="light"' in light.text

    css = client.get("/static/css/forge.css")
    assert css.status_code == 200
    assert "760px" in css.text
    assert ".tab.on" in css.text
    assert "var(--ember)" in css.text
    assert ".btn:disabled" in css.text

    js = client.get("/static/js/forge.js")
    assert js.status_code == 200
    assert "/api/prompts" in js.text
    assert "/api/pilot/sets" in js.text
    assert "/api/pilot/runs" in js.text
    assert "/api/pilot/promote" in js.text
    assert "can_promote" in js.text
    assert 'mode: mode' in js.text
    assert "batch_size" in js.text
    assert "batch=true" in js.text
    assert "reloadPromptForBatchSize" in js.text
    assert "formatPilotProgress" in js.text
    assert "progress" in js.text
    assert "setSaveFormOpen" in js.text
    assert "runAfterSave" in js.text
    assert "window.prompt" not in js.text


def _forge_pilot_metrics(*, f1: float, kappa: float, candidate: str) -> dict[str, Any]:
    return {
        "candidate_prompt": candidate,
        "candidate": {
            "accuracy": max(f1, 0.5),
            "precision": f1,
            "recall": f1,
            "f1": f1,
            "kappa": kappa,
            "confusion": {"tp": 10, "fp": 0, "fn": 0, "tn": 5},
        },
        "gates": {
            "passed": f1 >= 0.90 and kappa >= 0.80,
            "can_promote": f1 >= 0.90 and kappa >= 0.80,
            "failures": (
                (["f1"] if f1 < 0.90 else [])
                + (["kappa"] if kappa < 0.80 else [])
            ),
            "thresholds": {"f1_min": 0.90, "kappa_min": 0.80},
        },
    }


def _forge_config(
    tmp_path: Path,
    *,
    classifier: str = "default",
    registry_dir: Path | None = None,
) -> Path:
    prompts = [
        "[prompts]",
        f'classifier = "{classifier}"',
    ]
    if registry_dir is not None:
        prompts.append(f'registry_dir = "{registry_dir}"')
    path = tmp_path / "asgard.toml"
    path.write_text(
        "\n".join(
            [
                'repo = "owner/name"',
                f'output_dir = "{tmp_path / "out"}"',
                "",
                *prompts,
                "",
                "[llm.default]",
                'provider = "fake"',
                'model = "fake"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_forge_prompts_api(tmp_path: Path) -> None:
    app = create_app(dotenv=None)
    client = TestClient(app)
    registry = tmp_path / "prompts"
    role_q = f"role=classifier&registry_dir={registry}"

    listed = client.get(f"/api/prompts?{role_q}")
    assert listed.status_code == 200
    versions = listed.json()
    assert versions[0]["id"] == "default"
    assert "sha256" in versions[0]
    assert "body" not in versions[0]

    default = client.get(f"/api/prompts/classifier/default?registry_dir={registry}")
    assert default.status_code == 200
    assert default.json()["id"] == "default"
    assert default.json()["body"]
    assert default.json()["batch"] is False
    assert default.json()["sha256"] == content_sha256(default.json()["body"])

    from asgard.prompts.classifier import BATCH_SYSTEM_PROMPT, SYSTEM_PROMPT

    assert default.json()["body"] == SYSTEM_PROMPT
    batched = client.get(
        f"/api/prompts/classifier/default?registry_dir={registry}&batch=true"
    )
    assert batched.status_code == 200
    assert batched.json()["batch"] is True
    assert batched.json()["body"] == BATCH_SYSTEM_PROMPT
    assert batched.json()["body"] != default.json()["body"]

    saved = client.post(
        "/api/prompts/classifier",
        json={
            "name": "Forge Strict",
            "body": "classifier body for forge ui test\n",
            "notes": "from forge test",
            "registry_dir": str(registry),
        },
    )
    assert saved.status_code == 200
    payload = saved.json()
    assert payload["id"] == "forge-strict"
    assert payload["body"] == "classifier body for forge ui test\n"
    assert payload["notes"] == "from forge test"
    assert payload["sha256"] == content_sha256(payload["body"])

    again = client.get(f"/api/prompts?{role_q}")
    ids = [item["id"] for item in again.json()]
    assert ids[0] == "default"
    assert "forge-strict" in ids

    got = client.get(f"/api/prompts/classifier/forge-strict?registry_dir={registry}")
    assert got.status_code == 200
    assert got.json()["batch"] is False
    assert {key: value for key, value in got.json().items() if key != "batch"} == payload

    active = client.get("/api/prompts/active")
    assert active.status_code == 200
    assert active.json() == {
        "classifier": "default",
        "evaluator": "default",
        "synthesizer": "default",
    }

    config = _forge_config(tmp_path, classifier="forge-strict", registry_dir=registry)
    from_toml = client.get(f"/api/prompts/active?config={config}")
    assert from_toml.status_code == 200
    assert from_toml.json()["classifier"] == "forge-strict"


def test_forge_pilot_sets_and_promote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    app = create_app(dotenv=None)
    client = TestClient(app)

    sets = client.get("/api/pilot/sets")
    assert sets.status_code == 200
    bundled = sets.json()
    assert any(item["id"] == "example" and item["bundled"] is True for item in bundled)
    assert any(
        item["id"] == "binary_v2" and item["bundled"] is True and item["item_count"] == 15
        for item in bundled
    )

    example = load_pilot_set("example")
    upload_body = {
        "id": "ui-upload",
        "name": "UI Upload",
        "items": [example.items[0].model_dump(mode="json")],
    }
    uploaded = client.post("/api/pilot/sets", json=upload_body)
    assert uploaded.status_code == 200
    assert uploaded.json()["id"] == "ui-upload"
    upload_path = Path(uploaded.json()["path"])
    assert upload_path.is_file()
    assert upload_path.parent.name == "uploads"

    listed = client.get("/api/pilot/sets").json()
    assert any(item["id"] == "ui-upload" and item["bundled"] is False for item in listed)

    registry = PromptRegistry(tmp_path / "prompts")
    active = registry.save(
        "classifier",
        name="forge-active",
        body="active classifier prompt body\n",
    )
    candidate = registry.save(
        "classifier",
        name="forge-candidate",
        body="candidate classifier prompt body\n",
    )
    assert content_sha256(active.body) != content_sha256(candidate.body)

    config = _forge_config(
        tmp_path,
        classifier=active.id,
        registry_dir=registry.root,
    )
    before = config.read_text(encoding="utf-8")
    previous_hash = content_sha256(
        resolve_system_prompt(
            "classifier",
            {"prompts": {"classifier": active.id, "registry_dir": str(registry.root)}},
            tmp_path / "run",
        )
    )

    failing = tmp_path / "fail-metrics.json"
    failing.write_text(
        json.dumps(_forge_pilot_metrics(f1=0.50, kappa=0.40, candidate=candidate.id))
        + "\n",
        encoding="utf-8",
    )
    blocked = client.post(
        "/api/pilot/promote",
        json={
            "candidate": candidate.id,
            "config": str(config),
            "from_result": str(failing),
        },
    )
    assert blocked.status_code == 400
    assert "cannot promote" in blocked.json()["detail"]
    assert config.read_text(encoding="utf-8") == before
    assert f'classifier = "{active.id}"' in before

    passing = tmp_path / "ok-metrics.json"
    passing.write_text(
        json.dumps(_forge_pilot_metrics(f1=0.95, kappa=0.85, candidate=candidate.id))
        + "\n",
        encoding="utf-8",
    )
    promoted = client.post(
        "/api/pilot/promote",
        json={
            "candidate": candidate.id,
            "config": str(config),
            "from_result": str(passing),
        },
    )
    assert promoted.status_code == 200
    assert promoted.json()["can_promote"] is True

    text = config.read_text(encoding="utf-8")
    assert f'classifier = "{candidate.id}"' in text
    assert f'classifier = "{active.id}"' not in text

    new_hash = content_sha256(
        resolve_system_prompt(
            "classifier",
            {"prompts": {"classifier": candidate.id, "registry_dir": str(registry.root)}},
            tmp_path / "run",
        )
    )
    assert new_hash == content_sha256(candidate.body)
    assert new_hash != previous_hash
    assert previous_hash == content_sha256(active.body)


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


def test_council_file_endpoints(
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

    missing = client.get("/api/runs/missing/classify/output")
    assert missing.status_code == 404

    response = client.post("/api/runs", json={"config": str(source)})
    assert response.status_code == 200, response.text
    run_id = response.json()["id"]
    status = _wait_done(client, run_id)
    assert status["error"] is None

    disk_prefilter = json.loads((out / "prefilter" / "output.json").read_text(encoding="utf-8"))
    api_prefilter = client.get(f"/api/runs/{run_id}/prefilter/output")
    assert api_prefilter.status_code == 200
    assert api_prefilter.json() == disk_prefilter

    disk_mine = json.loads((out / "mine" / "output.json").read_text(encoding="utf-8"))
    api_mine = client.get(f"/api/runs/{run_id}/mine/output")
    assert api_mine.status_code == 200
    assert api_mine.json() == disk_mine
    assert isinstance(disk_mine, list) and disk_mine
    pr_number = disk_mine[0]
    disk_pr = json.loads((out / "mine" / "prs" / f"{pr_number}.json").read_text(encoding="utf-8"))
    api_pr = client.get(f"/api/runs/{run_id}/mine/prs/{pr_number}")
    assert api_pr.status_code == 200
    assert api_pr.json() == disk_pr
    assert "id" in api_pr.json()
    assert api_pr.json()["number"] == pr_number

    api_index = client.get(f"/api/runs/{run_id}/mine/index")
    assert api_index.status_code == 200
    index = api_index.json()
    assert index["numbers"] == disk_mine
    assert isinstance(index["node_ids"], dict)
    if isinstance(disk_pr.get("node_id"), str) and disk_pr["node_id"]:
        assert index["node_ids"][str(pr_number)] == disk_pr["node_id"]

    not_digit = client.get(f"/api/runs/{run_id}/mine/prs/abc")
    assert not_digit.status_code == 404
    traversal = client.get(f"/api/runs/{run_id}/mine/prs/..")
    assert traversal.status_code == 404

    disk_trace = (out / "classify" / "trace.jsonl").read_text(encoding="utf-8")
    api_trace = client.get(f"/api/runs/{run_id}/classify/trace")
    assert api_trace.status_code == 200
    assert "text/plain" in api_trace.headers.get("content-type", "")
    assert api_trace.text == disk_trace

    disk_items = (out / "classify" / "items.jsonl").read_text(encoding="utf-8")
    api_items = client.get(f"/api/runs/{run_id}/classify/items")
    assert api_items.status_code == 200
    assert "text/plain" in api_items.headers.get("content-type", "")
    assert api_items.text == disk_items

    disk_metrics = json.loads((out / "classify" / "metrics.json").read_text(encoding="utf-8"))
    api_metrics = client.get(f"/api/runs/{run_id}/classify/metrics")
    assert api_metrics.status_code == 200
    assert api_metrics.json() == disk_metrics
    assert {
        "approval_rate_iteration_1",
        "approval_rate_final",
        "mean_iterations",
    } <= set(api_metrics.json())

    disk_output = json.loads((out / "classify" / "output.json").read_text(encoding="utf-8"))
    api_output = client.get(f"/api/runs/{run_id}/classify/output")
    assert api_output.status_code == 200
    assert api_output.json() == disk_output
    assert isinstance(api_output.json(), list)
    assert api_output.json()
    assert {"pr_id", "status", "classification"} <= set(api_output.json()[0])


def test_guideline_file_endpoints_and_prompt(
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

    missing = client.get("/api/runs/missing/SECURITY.md")
    assert missing.status_code == 404

    response = client.post("/api/runs", json={"config": str(source)})
    assert response.status_code == 200, response.text
    run_id = response.json()["id"]
    status = _wait_done(client, run_id)
    assert status["error"] is None

    disk_md = (out / "SECURITY.md").read_text(encoding="utf-8")
    api_md = client.get(f"/api/runs/{run_id}/SECURITY.md")
    assert api_md.status_code == 200
    assert "text/markdown" in api_md.headers.get("content-type", "")
    assert api_md.text == disk_md
    disposition = api_md.headers.get("content-disposition", "")
    assert "SECURITY.md" in disposition

    disk_consolidate = json.loads(
        (out / "consolidate" / "output.json").read_text(encoding="utf-8")
    )
    api_consolidate = client.get(f"/api/runs/{run_id}/consolidate/output")
    assert api_consolidate.status_code == 200
    assert api_consolidate.json() == disk_consolidate

    disk_security_md = json.loads(
        (out / "security_md" / "output.json").read_text(encoding="utf-8")
    )
    api_security_md = client.get(f"/api/runs/{run_id}/security_md/output")
    assert api_security_md.status_code == 200
    assert api_security_md.json() == disk_security_md
    assert "unknown_citations" in api_security_md.json()

    prompt_get = client.get(f"/api/runs/{run_id}/synthesize/prompt")
    assert prompt_get.status_code == 200
    assert "text/plain" in prompt_get.headers.get("content-type", "")
    assert prompt_get.text == SYSTEM_PROMPT
    assert not (out / "synthesize" / "system_prompt.txt").exists()

    override = "Custom guideline for {repository}."
    put = client.put(
        f"/api/runs/{run_id}/synthesize/prompt",
        json={"text": override},
    )
    assert put.status_code == 200, put.text
    assert put.json()["path"] == "synthesize/system_prompt.txt"
    saved = out / "synthesize" / "system_prompt.txt"
    assert saved.is_file()
    assert saved.read_text(encoding="utf-8") == override
    prompt_again = client.get(f"/api/runs/{run_id}/synthesize/prompt")
    assert prompt_again.status_code == 200
    assert prompt_again.text == override

    bundle = client.get(f"/api/runs/{run_id}/bundle.zip")
    assert bundle.status_code == 200
    assert bundle.headers.get("content-type") == "application/zip"
    assert "bundle.zip" in bundle.headers.get("content-disposition", "")
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        names = set(archive.namelist())
    assert "SECURITY.md" in names
    assert "manifest.json" in names

    traversal = client.get(f"/api/runs/{run_id}/mine/prs/../SECURITY.md")
    assert traversal.status_code == 404
    traversal_dot = client.get(f"/api/runs/{run_id}/mine/prs/..")
    assert traversal_dot.status_code == 404


def _synthesis(security: int, *, title: str = "Authentication") -> str:
    return json.dumps(
        {
            "topics": [
                {
                    "title": title,
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


def test_guideline_regenerate_skips_classify(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    numbers = _numbers()
    security = numbers[0]
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    out = tmp_path / "out"
    source = _settings(tmp_path, out)
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
    handler = FakeHandler(
        [
            approved,
            verdict,
            rejected,
            verdict,
            _synthesis(security, title="Authentication"),
            _synthesis(security, title="Regenerated auth"),
        ]
    )
    app = create_app(
        opener=_opener(numbers),
        handlers={"fake": handler},
        dotenv=None,
    )
    client = TestClient(app)

    response = client.post("/api/runs", json={"config": str(source)})
    assert response.status_code == 200, response.text
    run_id = response.json()["id"]
    status = _wait_done(client, run_id)
    assert status["error"] is None
    calls_before = len(handler.calls)
    assert calls_before == 5
    events_before = len(_events(out / "events.jsonl"))
    md_before = (out / "SECURITY.md").read_text(encoding="utf-8")
    assert "Authentication" in md_before

    override = "Regenerate {repository} with a fresh prompt."
    put = client.put(
        f"/api/runs/{run_id}/synthesize/prompt",
        json={"text": override},
    )
    assert put.status_code == 200, put.text

    regenerated = client.post(
        f"/api/runs/{run_id}/regenerate",
        json={"from": "synthesize", "prompt": override},
    )
    assert regenerated.status_code == 200, regenerated.text
    status = _wait_done(client, run_id)
    assert status["error"] is None

    new_events = _events(out / "events.jsonl")[events_before:]
    started = [
        event["stage"]
        for event in new_events
        if event.get("type") == "StageStarted"
    ]
    assert "classify" not in started
    assert "synthesize" in started
    assert "security_md" in started
    assert "bundle" in started
    assert {
        "type": "StageFinished",
        "stage": "synthesize",
        "skipped": False,
    } in new_events

    new_calls = handler.calls[calls_before:]
    assert len(new_calls) == 1
    assert new_calls[0][0].content == override.format(repository="jpadilla/pyjwt")

    disk_md = (out / "SECURITY.md").read_text(encoding="utf-8")
    assert "Regenerated auth" in disk_md
    api_md = client.get(f"/api/runs/{run_id}/SECURITY.md")
    assert api_md.status_code == 200
    assert api_md.text == disk_md
    assert (out / "synthesize" / "system_prompt.txt").read_text(encoding="utf-8") == override


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


def test_setup_models_lists_available_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    def opener(request: urllib.request.Request, timeout: int = 10) -> Any:
        url = request.full_url
        path = url.split("?", 1)[0].rstrip("/")
        if path.endswith("/v1/models"):
            return _Response(
                json.dumps(
                    {
                        "data": [
                            {"id": "gpt-4.1-mini"},
                            {"id": "text-embedding-3-small"},
                        ]
                    }
                ).encode()
            )
        if "/api/tags" in url:
            return _Response(
                json.dumps({"models": [{"name": "llama3.1:latest"}]}).encode()
            )
        raise AssertionError(f"unexpected URL: {url}")

    app = create_app(opener=opener, dotenv=None)
    client = TestClient(app)

    openai = client.post(
        "/api/setup/models",
        json={"provider": "openai", "secrets": {"OPENAI_API_KEY": KEY}},
    )
    assert openai.status_code == 200, openai.text
    assert openai.json() == {
        "provider": "openai",
        "models": ["gpt-4.1-mini"],
    }

    ollama = client.post("/api/setup/models", json={"provider": "ollama"})
    assert ollama.status_code == 200, ollama.text
    assert ollama.json()["models"] == ["llama3.1:latest"]

    missing = client.post("/api/setup/models", json={"provider": "openai"})
    assert missing.status_code == 400

    setup = client.get("/setup")
    assert setup.status_code == 200
    assert 'name="model" id="model"' in setup.text or 'id="model"' in setup.text
    assert "<select" in setup.text
    assert "role-model" in setup.text


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
