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
from asgard.core.config import Filters, RunConfig
from asgard.core.events import PRMined, StageFinished, StageStarted
from asgard.core.orchestrator import Orchestrator
from asgard.core.stages.mine import MineStage
from asgard.providers.handlers.fake import FakeHandler
from asgard.providers.models import LLMSettings, RoleSpec

TOKEN = "ghp_secret"


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


def _config(directory: Path, **filters: object) -> RunConfig:
    return RunConfig(
        repo="jpadilla/pyjwt",
        github_token=TOKEN,
        llm=LLMSettings(default=RoleSpec(provider="fake", model="fake")),
        filters=Filters(**filters),
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


def _pull(number: int, day: str, updated_at: str = "2024-01-01T00:00:00Z") -> dict[str, object]:
    return {
        "number": number,
        "created_at": f"{day}T00:00:00Z",
        "updated_at": updated_at,
    }


def test_mine_stage_lists_caches_and_emits(tmp_path: Path) -> None:
    seen: list[str] = []
    updated = {"value": "2024-01-01T00:00:00Z"}

    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        url = request.full_url
        seen.append(url)
        assert request.get_header("Authorization") == f"Bearer {TOKEN}"
        if "/pulls?" in url:
            body = [_pull(7, "2024-03-01", updated["value"])]
            return _Response(json.dumps(body).encode())
        if url.endswith("/pulls/7"):
            return _Response(json.dumps({"number": 7, "body": "the change"}).encode())
        return _Response(b"[]")

    sink = _Sink()
    config = _config(tmp_path, state="closed", max_prs=5)
    result = Orchestrator([MineStage(opener=opener)], [sink]).run(tmp_path, data=config)
    assert result == [7]
    assert json.loads((tmp_path / "mine" / "output.json").read_text(encoding="utf-8")) == [7]
    saved = json.loads((tmp_path / "mine" / "prs" / "7.json").read_text(encoding="utf-8"))
    assert saved["body"] == "the change"
    assert saved["updated_at"] == "2024-01-01T00:00:00Z"
    assert "state=closed" in seen[0]
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
    assert seen[-1].split("?", 1)[0].endswith("/pulls")

    updated["value"] = "2024-02-01T00:00:00Z"
    (tmp_path / "mine" / "output.json").unlink()
    before = len(seen)
    Orchestrator([MineStage(opener=opener)]).run(tmp_path, data=config)
    assert len(seen) > before + 1
    saved = json.loads((tmp_path / "mine" / "prs" / "7.json").read_text(encoding="utf-8"))
    assert saved["updated_at"] == "2024-02-01T00:00:00Z"


def test_mine_stage_applies_date_filters(tmp_path: Path) -> None:
    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        url = request.full_url
        if "/pulls?" in url:
            assert "state=open" in url
            body = [
                _pull(3, "2024-03-01"),
                _pull(2, "2024-02-01"),
                _pull(1, "2024-01-01"),
            ]
            return _Response(json.dumps(body).encode())
        if url.endswith("/pulls/2"):
            return _Response(json.dumps({"number": 2, "body": "kept"}).encode())
        return _Response(b"[]")

    config = _config(
        tmp_path,
        state="open",
        since=date(2024, 1, 15),
        until=date(2024, 2, 15),
    )
    assert Orchestrator([MineStage(opener=opener)]).run(tmp_path, data=config) == [2]


def test_empty_repository_writes_an_empty_list(tmp_path: Path) -> None:
    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        assert "/pulls?" in request.full_url
        return _Response(b"[]")

    sink = _Sink()
    result = Orchestrator([MineStage(opener=opener)], [sink]).run(
        tmp_path,
        data=_config(tmp_path),
    )
    assert result == []
    assert json.loads((tmp_path / "mine" / "output.json").read_text(encoding="utf-8")) == []
    assert list((tmp_path / "mine" / "prs").glob("*.json")) == []
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
        if "/pulls?" in url:
            assert "state=closed" in url
            body = [_pull(4, "2024-01-01")]
            return _Response(json.dumps(body).encode())
        if url.endswith("/pulls/4"):
            return _Response(json.dumps({"number": 4, "body": "change"}).encode())
        return _Response(b"[]")

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
    events = [
        json.loads(line) for line in (out / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert [event["type"] for event in events] == [
        "StageStarted",
        "PRMined",
        "StageFinished",
        "StageStarted",
        "StageFinished",
        "StageStarted",
        "ClassificationAttempt",
        "EvaluatorVerdict",
        "LoopFinished",
        "StageFinished",
        "StageStarted",
        "StageFinished",
    ]
    assert events[0]["stage"] == "mine"
    assert events[1]["pr_id"] == "4"
    assert events[3]["stage"] == "normalize"
    assert events[4] == {"type": "StageFinished", "stage": "normalize", "skipped": False}
    assert events[5]["stage"] == "classify"
    assert events[9] == {"type": "StageFinished", "stage": "classify", "skipped": False}
    assert events[10]["stage"] == "consolidate"
    assert events[11] == {"type": "StageFinished", "stage": "consolidate", "skipped": False}
    normalized = json.loads((out / "normalize" / "output.json").read_text(encoding="utf-8"))
    assert normalized[0]["description"] == "change"
    assert normalized[0]["context"]["repository"] == "jpadilla/pyjwt"
    assert normalized[0]["context"]["pr_id"] == "4"
    captured = capsys.readouterr()
    assert captured.out == ""
    assert TOKEN not in captured.err
    assert TOKEN not in (out / "events.jsonl").read_text(encoding="utf-8")

    calls = len(seen)
    main(
        ["run", "--config", str(out / "config.json")],
        opener=opener,
        handlers={"fake": _approved_handler()},
    )
    assert len(seen) == calls + 1
    assert not seen[-1].endswith("/pulls/4")
    finished = [
        json.loads(line) for line in (out / "events.jsonl").read_text(encoding="utf-8").splitlines()
    ][-4:]
    assert finished == [
        {"type": "StageFinished", "stage": "mine", "skipped": True},
        {"type": "StageFinished", "stage": "normalize", "skipped": True},
        {"type": "StageFinished", "stage": "classify", "skipped": True},
        {"type": "StageFinished", "stage": "consolidate", "skipped": True},
    ]

    calls = len(seen)
    main(
        ["run", "--config", str(out / "config.json"), "--from", "consolidate"],
        opener=opener,
        handlers={"fake": _approved_handler()},
    )
    assert len(seen) == calls + 1
    assert not seen[-1].endswith("/pulls/4")
    resumed = json.loads((out / "events.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert resumed == {"type": "StageFinished", "stage": "consolidate", "skipped": True}


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
        if "/pulls?" in request.full_url:
            return _Response(json.dumps([_pull(4, "2024-01-01")]).encode())
        if request.full_url.endswith("/pulls/4"):
            return _Response(json.dumps({"number": 4, "body": "change"}).encode())
        return _Response(b"[]")

    with pytest.raises(SystemExit) as caught:
        main(
            ["run", "--config", str(source), "--from", "normalize"],
            opener=opener,
            handlers={"fake": _approved_handler()},
        )
    assert caught.value.code == 2
    assert "previous stage is not done: mine" in capsys.readouterr().err
    assert not any("/pulls" in url for url in seen)

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
    assert "/pulls" not in seen[-1]
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
    assert os.environ["GITHUB_TOKEN"] not in (out / "config.json").read_text(encoding="utf-8")
