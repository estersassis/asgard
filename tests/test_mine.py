"""Mine stage and ``asgard run``, without the network.

A live check against a small public repository is marked ``smoke`` and stays
out of the default pytest run:

    GITHUB_TOKEN=... pytest tests/test_mine.py -m smoke
"""

import io
import json
import os
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path

import pytest

from asgard.cli.main import main
from asgard.core.config import Filters, MiningSettings, RunConfig
from asgard.core.events import PRMined, StageFinished, StageStarted
from asgard.core.orchestrator import Orchestrator
from asgard.core.stages.mine import MineStage
from asgard.providers.handlers.fake import FakeHandler
from asgard.providers.models import LLMSettings, RoleSpec

TOKEN = "ghp_secret"
_GRAPHQL = "https://api.github.com/graphql"


def _events(path: Path) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        event.pop("at", None)
        events.append(event)
    return events


class _Response:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = headers or {"X-OAuth-Scopes": "repo"}

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


class _Sink:
    def __init__(self) -> None:
        self.events: list[object] = []

    def handle(self, event: object) -> None:
        self.events.append(event)


def _config(directory: Path, *, page_size: int = 50, **filters: object) -> RunConfig:
    return RunConfig(
        repo="jpadilla/pyjwt",
        github_token=TOKEN,
        llm=LLMSettings(default=RoleSpec(provider="fake", model="fake")),
        filters=Filters(**filters),
        mining=MiningSettings(page_size=page_size),
        output_dir=directory,
    )


def _approved_handler() -> FakeHandler:
    classification = json.dumps(
        {
            "has_security_discussion": False,
            "evidence": [],
            "rationale": "checked",
        }
    )
    findings = [
        {
            "criterion": criterion,
            "passed": True,
            "explanation": f"{criterion} checked",
        }
        for criterion in ("decision", "evidence", "completeness", "rationale")
    ]
    verdict = json.dumps({"verdict": "APPROVE", "findings": findings, "feedback": ""})
    return FakeHandler([classification, verdict])


def _empty_connection() -> dict[str, object]:
    return {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}


def _node(
    number: int,
    day: str,
    *,
    updated_at: str = "2024-01-01T00:00:00Z",
    body: str = "",
    state: str = "OPEN",
    merged: bool = False,
    title: str = "",
) -> dict[str, object]:
    return {
        "id": f"PR_node_{number}",
        "number": number,
        "title": title or f"pull {number}",
        "body": body,
        "state": state,
        "merged": merged,
        "createdAt": f"{day}T00:00:00Z",
        "updatedAt": updated_at,
        "baseRepository": {"nameWithOwner": "jpadilla/pyjwt"},
        "comments": _empty_connection(),
        "reviewThreads": _empty_connection(),
    }


def _page(
    nodes: list[dict[str, object]],
    *,
    has_next: bool = False,
    cursor: str | None = None,
) -> bytes:
    return json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequests": {
                        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
                        "nodes": nodes,
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


def _graphql_body(request: urllib.request.Request) -> dict[str, object]:
    raw = request.data
    assert isinstance(raw, bytes)
    payload = json.loads(raw.decode())
    assert isinstance(payload, dict)
    return payload


def _is_repo_check(url: str) -> bool:
    return url.startswith("https://api.github.com/repos/") and "/pulls" not in url


def test_mine_stage_lists_caches_and_emits(tmp_path: Path) -> None:
    seen: list[str] = []
    updated = {"value": "2024-01-01T00:00:00Z"}

    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        url = request.full_url
        seen.append(url)
        assert request.get_header("Authorization") == f"Bearer {TOKEN}"
        assert url == _GRAPHQL
        body = _graphql_body(request)
        assert body["variables"]["states"] == ["CLOSED", "MERGED"]
        assert body["variables"]["pageSize"] == 50
        return _Response(
            _page(
                [
                    _node(
                        7,
                        "2024-03-01",
                        updated_at=updated["value"],
                        body="the change",
                        state="CLOSED",
                    )
                ]
            )
        )

    sink = _Sink()
    config = _config(tmp_path, state="closed", max_prs=5)
    result = Orchestrator([MineStage(opener=opener)], [sink]).run(tmp_path, data=config)
    assert result == [7]
    assert json.loads((tmp_path / "mine" / "output.json").read_text(encoding="utf-8")) == [7]
    saved = json.loads((tmp_path / "mine" / "prs" / "7.json").read_text(encoding="utf-8"))
    assert saved["body"] == "the change"
    assert saved["updated_at"] == "2024-01-01T00:00:00Z"
    assert "timeline_items" in saved
    metrics = json.loads((tmp_path / "mine" / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["api_requests"] == 1
    assert metrics["page_size"] == 50
    assert metrics["graphql_cost_total"] == 1
    assert metrics["overflow_followups"] == 0
    assert metrics["page_size_reductions"] == 0
    assert isinstance(metrics["duration_s"], float)
    mined = [event for event in sink.events if isinstance(event, PRMined)]
    assert [event.pr_id for event in mined] == ["7"]
    assert isinstance(sink.events[0], StageStarted)
    assert isinstance(sink.events[-1], StageFinished)

    listed = len(seen)
    sink.events.clear()
    again = Orchestrator([MineStage(opener=opener)], [sink]).run(tmp_path, data=config)
    assert again == [7]
    assert seen == seen[:listed]
    assert sink.events == [StageFinished(stage="mine", skipped=True)]

    (tmp_path / "mine" / "output.json").unlink()
    Orchestrator([MineStage(opener=opener)]).run(tmp_path, data=config)
    assert len(seen) == listed + 1
    assert seen[-1] == _GRAPHQL

    updated["value"] = "2024-02-01T00:00:00Z"
    (tmp_path / "mine" / "output.json").unlink()
    before = len(seen)
    Orchestrator([MineStage(opener=opener)]).run(tmp_path, data=config)
    assert len(seen) == before + 1
    saved = json.loads((tmp_path / "mine" / "prs" / "7.json").read_text(encoding="utf-8"))
    assert saved["updated_at"] == "2024-02-01T00:00:00Z"


def test_mine_stage_honors_page_size(tmp_path: Path) -> None:
    sizes: list[int] = []

    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        body = _graphql_body(request)
        sizes.append(int(body["variables"]["pageSize"]))
        return _Response(_page([]))

    Orchestrator([MineStage(opener=opener)]).run(tmp_path, data=_config(tmp_path, page_size=10))
    assert sizes == [10]
    metrics = json.loads((tmp_path / "mine" / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["page_size"] == 10


def test_mine_stage_applies_date_filters(tmp_path: Path) -> None:
    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        body = _graphql_body(request)
        assert body["variables"]["states"] == ["OPEN"]
        return _Response(
            _page(
                [
                    _node(3, "2024-03-01", body="new"),
                    _node(2, "2024-02-01", body="kept"),
                    _node(1, "2024-01-01", body="old"),
                ]
            )
        )

    config = _config(
        tmp_path,
        state="open",
        since=date(2024, 1, 15),
        until=date(2024, 2, 15),
    )
    assert Orchestrator([MineStage(opener=opener)]).run(tmp_path, data=config) == [2]
    saved = json.loads((tmp_path / "mine" / "prs" / "2.json").read_text(encoding="utf-8"))
    assert saved["body"] == "kept"


def test_empty_repository_writes_an_empty_list(tmp_path: Path) -> None:
    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        assert request.full_url == _GRAPHQL
        return _Response(_page([]))

    sink = _Sink()
    result = Orchestrator([MineStage(opener=opener)], [sink]).run(
        tmp_path,
        data=_config(tmp_path),
    )
    assert result == []
    assert json.loads((tmp_path / "mine" / "output.json").read_text(encoding="utf-8")) == []
    assert list((tmp_path / "mine" / "prs").glob("*.json")) == []
    metrics = json.loads((tmp_path / "mine" / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["api_requests"] == 1
    assert not any(isinstance(event, PRMined) for event in sink.events)


def test_mine_error_does_not_echo_the_token(tmp_path: Path) -> None:
    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "no",
            hdrs=None,
            fp=io.BytesIO(TOKEN.encode()),
        )

    sink = _Sink()
    with pytest.raises(Exception, match="invalid GitHub token") as caught:
        Orchestrator([MineStage(opener=opener)], [sink]).run(tmp_path, data=_config(tmp_path))
    assert TOKEN not in str(caught.value)
    assert all(TOKEN not in getattr(event, "message", "") for event in sink.events)


def test_run_mines_after_saving_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setattr("asgard.core.config.load_dotenv", lambda path=Path(".env"): None)
    out = tmp_path / "out"
    source = tmp_path / "asgard.toml"
    source.write_text(
        "\n".join(
            [
                'repo = "jpadilla/pyjwt"',
                f'output_dir = "{out}"',
                "",
                "[filters]",
                'state = "closed"',
                "",
                "[llm.default]",
                'provider = "fake"',
                'model = "fake"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    seen: list[str] = []

    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        url = request.full_url
        seen.append(url)
        if _is_repo_check(url):
            return _Response(b"{}")
        assert url == _GRAPHQL
        body = _graphql_body(request)
        assert body["variables"]["states"] == ["CLOSED", "MERGED"]
        return _Response(
            _page([_node(4, "2024-01-01", body="change", state="CLOSED")])
        )

    main(
        ["run", "--config", str(source), "--max-prs", "1"],
        opener=opener,
        handlers={"fake": _approved_handler()},
    )
    saved = json.loads((out / "config.json").read_text(encoding="utf-8"))
    assert saved["repo"] == "jpadilla/pyjwt"
    assert saved["filters"]["max_prs"] == 1
    assert TOKEN not in json.dumps(saved)
    assert json.loads((out / "mine" / "output.json").read_text(encoding="utf-8")) == [4]
    cached = json.loads((out / "mine" / "prs" / "4.json").read_text(encoding="utf-8"))
    assert cached["body"] == "change"
    metrics = json.loads((out / "mine" / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["api_requests"] >= 1
    events = _events(out / "events.jsonl")
    assert [event["type"] for event in events] == [
        "StageStarted",
        "PRMined",
        "StageFinished",
        "StageStarted",
        "StageFinished",
        "StageStarted",
        "StageFinished",
        "StageStarted",
        "ClassifyProgress",
        "ClassificationAttempt",
        "EvaluatorVerdict",
        "LoopFinished",
        "StageFinished",
        "StageStarted",
        "StageFinished",
        "StageStarted",
        "StageFinished",
        "StageStarted",
        "StageFinished",
        "StageStarted",
        "StageFinished",
    ]
    assert events[0]["stage"] == "mine"
    assert events[1]["pr_id"] == "4"
    assert events[3]["stage"] == "normalize"
    assert events[4] == {"type": "StageFinished", "stage": "normalize", "skipped": False}
    assert events[5] == {"type": "StageStarted", "stage": "prefilter", "total": 1}
    assert events[6] == {"type": "StageFinished", "stage": "prefilter", "skipped": False}
    assert events[7]["stage"] == "classify"
    assert events[8] == {"type": "ClassifyProgress", "kind": "pull", "index": 1, "total": 1}
    assert events[12] == {"type": "StageFinished", "stage": "classify", "skipped": False}
    assert events[13]["stage"] == "consolidate"
    assert events[14] == {"type": "StageFinished", "stage": "consolidate", "skipped": False}
    assert events[15]["stage"] == "synthesize"
    assert events[16] == {"type": "StageFinished", "stage": "synthesize", "skipped": False}
    assert events[17]["stage"] == "security_md"
    assert events[18] == {"type": "StageFinished", "stage": "security_md", "skipped": False}
    assert events[19]["stage"] == "bundle"
    assert events[20] == {"type": "StageFinished", "stage": "bundle", "skipped": False}
    assert not any(event["type"] == "SynthesisChunk" for event in events)
    assert not any(event["type"] == "CitationWarning" for event in events)
    guideline = json.loads((out / "synthesize" / "output.json").read_text(encoding="utf-8"))
    assert guideline == "There was no security discussion, so there is no guideline."
    security = (out / "SECURITY.md").read_text(encoding="utf-8")
    assert guideline in security
    assert "jpadilla/pyjwt" in security
    assert "Pull requests analysed: 1. Security pull requests used: 0." in security
    manifest = json.loads((out / "security_md" / "output.json").read_text(encoding="utf-8"))
    assert manifest["unknown_citations"] == []
    assert Path(manifest["path"]) == out / "SECURITY.md"
    kept = json.loads((out / "prefilter" / "output.json").read_text(encoding="utf-8"))
    assert [item["context"]["pr_id"] for item in kept] == ["4"]
    removed = json.loads((out / "prefilter" / "removed.json").read_text(encoding="utf-8"))
    assert removed == {"before": 1, "after": 1, "removed": []}
    normalized = json.loads((out / "normalize" / "output.json").read_text(encoding="utf-8"))
    assert normalized[0]["description"] == "change"
    assert normalized[0]["context"]["repository"] == "jpadilla/pyjwt"
    assert normalized[0]["context"]["pr_id"] == "4"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert TOKEN not in captured.err
    assert TOKEN not in (out / "events.jsonl").read_text(encoding="utf-8")
    manifest_text = (out / "manifest.json").read_text(encoding="utf-8")
    assert TOKEN not in manifest_text
    recorded = json.loads(manifest_text)
    on_disk = {
        path.relative_to(out).as_posix()
        for path in out.rglob("*")
        if path.is_file()
    }
    assert on_disk == set(recorded["files"])
    assert recorded["files"]["mine/metrics.json"] == "GraphQL mining request and cost metrics."
    assert not (out / "bundle" / "classifications.xlsx").exists()

    calls = len(seen)
    main(
        ["run", "--config", str(out / "config.json")],
        opener=opener,
        handlers={"fake": _approved_handler()},
    )
    assert len(seen) == calls + 1
    assert seen[-1] != _GRAPHQL
    finished = _events(out / "events.jsonl")[-8:]
    assert finished == [
        {"type": "StageFinished", "stage": "mine", "skipped": True},
        {"type": "StageFinished", "stage": "normalize", "skipped": True},
        {"type": "StageFinished", "stage": "prefilter", "skipped": True},
        {"type": "StageFinished", "stage": "classify", "skipped": True},
        {"type": "StageFinished", "stage": "consolidate", "skipped": True},
        {"type": "StageFinished", "stage": "synthesize", "skipped": True},
        {"type": "StageFinished", "stage": "security_md", "skipped": True},
        {"type": "StageFinished", "stage": "bundle", "skipped": True},
    ]

    calls = len(seen)
    main(
        ["run", "--config", str(out / "config.json"), "--from", "consolidate"],
        opener=opener,
        handlers={"fake": _approved_handler()},
    )
    assert len(seen) == calls + 1
    assert seen[-1] != _GRAPHQL
    resumed = _events(out / "events.jsonl")[-4:]
    assert resumed == [
        {"type": "StageFinished", "stage": "consolidate", "skipped": True},
        {"type": "StageFinished", "stage": "synthesize", "skipped": True},
        {"type": "StageFinished", "stage": "security_md", "skipped": True},
        {"type": "StageFinished", "stage": "bundle", "skipped": True},
    ]

    calls = len(seen)
    main(
        ["run", "--config", str(out / "config.json"), "--from", "synthesize"],
        opener=opener,
        handlers={"fake": _approved_handler()},
    )
    assert len(seen) == calls + 1
    assert seen[-1] != _GRAPHQL
    resumed_synthesis = _events(out / "events.jsonl")[-3:]
    assert resumed_synthesis == [
        {"type": "StageFinished", "stage": "synthesize", "skipped": True},
        {"type": "StageFinished", "stage": "security_md", "skipped": True},
        {"type": "StageFinished", "stage": "bundle", "skipped": True},
    ]

    calls = len(seen)
    main(
        ["run", "--config", str(out / "config.json"), "--from", "security_md"],
        opener=opener,
        handlers={"fake": _approved_handler()},
    )
    assert len(seen) == calls + 1
    assert seen[-1] != _GRAPHQL
    resumed_security = _events(out / "events.jsonl")[-2:]
    assert resumed_security == [
        {"type": "StageFinished", "stage": "security_md", "skipped": True},
        {"type": "StageFinished", "stage": "bundle", "skipped": True},
    ]

    calls = len(seen)
    main(
        ["run", "--config", str(out / "config.json"), "--from", "bundle"],
        opener=opener,
        handlers={"fake": _approved_handler()},
    )
    assert len(seen) == calls + 1
    resumed_bundle = _events(out / "events.jsonl")[-1]
    assert resumed_bundle == {"type": "StageFinished", "stage": "bundle", "skipped": True}


def test_run_from_normalize_uses_the_saved_mine(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setattr("asgard.core.config.load_dotenv", lambda path=Path(".env"): None)
    out = tmp_path / "out"
    source = tmp_path / "asgard.toml"
    source.write_text(
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
    seen: list[str] = []

    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        seen.append(request.full_url)
        if _is_repo_check(request.full_url):
            return _Response(b"{}")
        assert request.full_url == _GRAPHQL
        return _Response(_page([_node(4, "2024-01-01", body="change")]))

    with pytest.raises(SystemExit) as caught:
        main(
            ["run", "--config", str(source), "--from", "normalize"],
            opener=opener,
            handlers={"fake": _approved_handler()},
        )
    assert caught.value.code == 2
    assert "previous stage is not done: mine" in capsys.readouterr().err
    assert _GRAPHQL not in seen

    main(
        ["run", "--config", str(source), "--max-prs", "1"],
        opener=opener,
        handlers={"fake": _approved_handler()},
    )
    normalized = json.loads((out / "normalize" / "output.json").read_text(encoding="utf-8"))
    (out / "normalize" / "output.json").unlink()
    (out / "normalize" / "items.jsonl").unlink()
    calls = len(seen)
    main(
        ["run", "--config", str(out / "config.json"), "--from", "normalize"],
        opener=opener,
        handlers={"fake": _approved_handler()},
    )
    assert len(seen) == calls + 1
    assert seen[-1] != _GRAPHQL
    assert json.loads((out / "normalize" / "output.json").read_text(encoding="utf-8")) == normalized


@pytest.mark.smoke
@pytest.mark.skipif(
    not os.getenv("GITHUB_TOKEN"),
    reason="Set GITHUB_TOKEN to mine a public repository",
)
def test_live_public_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Live check: mine one pull request from a small public repository.

    Excluded from the default pytest run (``-m 'not smoke'``). Run it with:

        GITHUB_TOKEN=... pytest tests/test_mine.py -m smoke
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("asgard.core.config.load_dotenv", lambda path=Path(".env"): None)
    out = tmp_path / "out"
    source = tmp_path / "asgard.toml"
    source.write_text(
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
    main(
        [
            "run",
            "--config",
            str(source),
            "--max-prs",
            "1",
            "--provider",
            "fake",
            "--model",
            "fake",
        ],
        handlers={"fake": _approved_handler()},
    )
    numbers = json.loads((out / "mine" / "output.json").read_text(encoding="utf-8"))
    assert len(numbers) == 1
    assert isinstance(numbers[0], int)
    assert (out / "mine" / "prs" / f"{numbers[0]}.json").is_file()
    assert (out / "mine" / "metrics.json").is_file()
    assert os.environ["GITHUB_TOKEN"] not in (out / "config.json").read_text(encoding="utf-8")
