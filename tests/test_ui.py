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

from asgard.core.events import ClassificationAttempt, Error
from asgard.core.orchestrator import Orchestrator
from asgard.core.stage import CancelledError, RunContext, Stage
from asgard.providers.handlers.fake import FakeHandler
from asgard.ui.app import create_app
from asgard.ui.sink import UISink

TOKEN = "ghp_secret"
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

    light = client.get("/setup?theme=light")
    assert light.status_code == 200
    assert 'data-theme="light"' in light.text

    tokens = client.get("/static/css/tokens.css")
    assert tokens.status_code == 200
    assert "--stone:" in tokens.text
    assert '--ember: #D68C42' in tokens.text or "--ember:#D68C42" in tokens.text.replace(" ", "")
    assert 'data-theme="light"' in tokens.text

    templates = Path(__file__).resolve().parents[1] / "asgard" / "ui" / "templates"
    for path in templates.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        # Hex colors belong in tokens.css only.
        assert not _TOKEN_HEX.search(text), path


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
