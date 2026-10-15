"""CLI controls: one stage, export, log modes, and exit codes. No network."""

import json
import urllib.request
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from asgard.cli.logs import show_logs
from asgard.cli.main import _PIPELINE, main
from asgard.providers.handlers.fake import FakeHandler

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


def _handler() -> FakeHandler:
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


def _pull(number: int) -> dict[str, object]:
    return {
        "number": number,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-01T00:00:00Z",
    }


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


def _opener(seen: list[str], *, fail_pulls: bool = False):
    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        url = request.full_url
        seen.append(url)
        if "/pulls?" in url:
            if fail_pulls:
                raise RuntimeError("disk full")
            return _Response(json.dumps([_pull(4)]).encode())
        if url.endswith("/pulls/4"):
            return _Response(json.dumps({"number": 4, "body": "change"}).encode())
        return _Response(b"[]")

    return opener


def _prepare(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, list[str]]:
    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setattr("asgard.core.config.load_dotenv", lambda path=Path(".env"): None)
    out = tmp_path / "out"
    return _settings(tmp_path, out), out, []


def _events(path: Path, start: int = 0) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines()[start:]:
        event = json.loads(line)
        event.pop("at", None)
        events.append(event)
    return events


def test_only_reruns_one_finished_stage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, out, seen = _prepare(tmp_path, monkeypatch)
    opener = _opener(seen)
    main(
        ["run", "--config", str(source), "--max-prs", "1", "--quiet"],
        opener=opener,
        handlers={"fake": _handler()},
    )
    log = out / "events.jsonl"
    for stage in _PIPELINE:
        start = len(log.read_text(encoding="utf-8").splitlines())
        main(
            ["run", "--config", str(out / "config.json"), "--only", stage, "--quiet"],
            opener=opener,
            handlers={"fake": _handler()},
        )
        assert _events(log, start) == [
            {"type": "StageFinished", "stage": stage, "skipped": True}
        ]


def test_only_normalize_does_not_mine_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, out, seen = _prepare(tmp_path, monkeypatch)
    opener = _opener(seen)
    main(
        ["run", "--config", str(source), "--max-prs", "1", "--quiet"],
        opener=opener,
        handlers={"fake": _handler()},
    )
    saved = json.loads((out / "normalize" / "output.json").read_text(encoding="utf-8"))
    (out / "normalize" / "output.json").unlink()
    (out / "normalize" / "items.jsonl").unlink()
    calls = len(seen)
    main(
        ["run", "--config", str(out / "config.json"), "--only", "normalize", "--quiet"],
        opener=opener,
        handlers={"fake": _handler()},
    )
    assert not any("/pulls" in url for url in seen[calls:])
    assert json.loads((out / "normalize" / "output.json").read_text(encoding="utf-8")) == saved


def test_from_and_only_together_exit_2() -> None:
    with pytest.raises(SystemExit) as caught:
        main(["run", "--from", "mine", "--only", "mine"])
    assert caught.value.code == 2


def test_quiet_hides_progress_and_json_logs_go_to_stdout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, out, seen = _prepare(tmp_path, monkeypatch)
    main(
        ["run", "--config", str(source), "--max-prs", "1", "--quiet", "--json-logs"],
        opener=_opener(seen),
        handlers={"fake": _handler()},
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert TOKEN not in captured.out
    events = [json.loads(line) for line in captured.out.splitlines()]
    assert events[0]["type"] == "StageStarted"
    assert events[0]["stage"] == "mine"
    assert events[-1] == {
        "type": "StageFinished",
        "stage": "bundle",
        "skipped": False,
        "at": events[-1]["at"],
    }
    assert (out / "SECURITY.md").is_file()
    assert (out / "manifest.json").is_file()
    assert (out / "events.jsonl").is_file()


def test_pipeline_failure_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, _out, seen = _prepare(tmp_path, monkeypatch)
    with pytest.raises(SystemExit) as caught:
        main(
            ["run", "--config", str(source), "--max-prs", "1", "--quiet"],
            opener=_opener(seen, fail_pulls=True),
            handlers={"fake": _handler()},
        )
    assert caught.value.code == 1
    captured = capsys.readouterr()
    assert captured.err == "disk full\n"
    assert "Traceback" not in captured.err


def test_export_writes_the_spreadsheet_without_rewriting_the_manifest(tmp_path: Path) -> None:
    classified = tmp_path / "classify" / "output.json"
    classified.parent.mkdir()
    classified.write_text(
        json.dumps(
            [
                {
                    "pr_id": "4",
                    "status": "approved",
                    "iterations": 1,
                    "classification": {
                        "has_security_discussion": False,
                        "evidence": [],
                        "rationale": "checked",
                    },
                    "evaluation": {"verdict": "APPROVE"},
                }
            ]
        ),
        encoding="utf-8",
    )
    marker = '{"files": {}}\n'
    (tmp_path / "manifest.json").write_text(marker, encoding="utf-8")

    main(["export", str(tmp_path)])

    assert (tmp_path / "bundle" / "classifications.xlsx").is_file()
    assert (tmp_path / "manifest.json").read_text(encoding="utf-8") == marker


def test_export_without_classifications_exits_2(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["export", str(tmp_path)])
    assert caught.value.code == 2
    assert "classify output" in capsys.readouterr().err


def _console() -> tuple[Console, StringIO]:
    stream = StringIO()
    console = Console(file=stream, force_terminal=False, no_color=True, width=100)
    return console, stream


def _dump(events: list[dict[str, object]]) -> str:
    return "".join(json.dumps(event) + "\n" for event in events)


def _sample_log() -> list[dict[str, object]]:
    body = "do not print this body"
    return [
        {"type": "StageStarted", "stage": "mine", "at": "2026-10-15T00:00:00+00:00"},
        {"type": "PRMined", "pr_id": "7", "at": "2026-10-15T00:00:01+00:00"},
        {
            "type": "StageFinished",
            "stage": "mine",
            "skipped": False,
            "at": "2026-10-15T00:00:02+00:00",
        },
        {"type": "StageStarted", "stage": "classify", "at": "2026-10-15T00:00:00+00:00"},
        {"type": "ClassificationAttempt", "pr_id": "42", "at": "2026-10-15T00:00:10+00:00"},
        {
            "type": "EvaluatorVerdict",
            "pr_id": "42",
            "approved": True,
            "at": "2026-10-15T00:00:20+00:00",
        },
        {
            "type": "EvaluatorVerdict",
            "pr_id": "43",
            "approved": False,
            "at": "2026-10-15T00:00:30+00:00",
        },
        {"type": "LoopFinished", "pr_id": "42", "at": "2026-10-15T00:00:40+00:00"},
        {
            "type": "StageFinished",
            "stage": "classify",
            "skipped": False,
            "at": "2026-10-15T00:01:04+00:00",
        },
        {"type": "StageStarted", "stage": "synthesize", "at": "2026-10-15T00:02:00+00:00"},
        {"type": "SynthesisChunk", "text": body, "at": "2026-10-15T00:02:01+00:00"},
        {
            "type": "StageFinished",
            "stage": "synthesize",
            "skipped": False,
            "at": "2026-10-15T00:02:02+00:00",
        },
    ]


def test_logs_groups_stages_and_hides_the_guideline_body(tmp_path: Path) -> None:
    (tmp_path / "events.jsonl").write_text(_dump(_sample_log()), encoding="utf-8")
    console, stream = _console()

    show_logs(tmp_path, console=console)

    text = stream.getvalue()
    assert "mine" in text
    assert "classify" in text
    assert "synthesize" in text
    assert "approved" in text
    assert "revise" in text
    assert "42" in text
    assert "43" in text
    assert "4 events" in text
    assert "1m 4s" in text
    assert "guideline draft, 22 characters" in text
    assert "do not print this body" not in text


def test_logs_stage_filter_omits_other_stages(tmp_path: Path) -> None:
    (tmp_path / "events.jsonl").write_text(_dump(_sample_log()), encoding="utf-8")
    console, stream = _console()

    show_logs(tmp_path, stage="classify", console=console)

    text = stream.getvalue()
    assert "classify" in text
    assert "approved" in text
    assert "revise" in text
    assert "mined" not in text
    assert "synthesize" not in text
    assert "7" not in text


def test_logs_missing_file_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main(["logs", str(tmp_path)])
    assert caught.value.code == 2
    assert "events.jsonl" in capsys.readouterr().err


def test_logs_invalid_json_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "events.jsonl").write_text("not json\n", encoding="utf-8")
    with pytest.raises(SystemExit) as caught:
        main(["logs", str(tmp_path)])
    assert caught.value.code == 2
    assert "not valid json" in capsys.readouterr().err


def test_logs_show_the_batch_and_the_pull_request_place(tmp_path: Path) -> None:
    (tmp_path / "events.jsonl").write_text(
        _dump(
            [
                {"type": "StageStarted", "stage": "classify", "at": "2026-10-15T00:00:00+00:00"},
                {
                    "type": "ClassifyProgress",
                    "kind": "batch",
                    "index": 2,
                    "total": 12,
                    "at": "2026-10-15T00:00:01+00:00",
                },
                {
                    "type": "ClassificationAttempt",
                    "pr_id": "42",
                    "at": "2026-10-15T00:00:02+00:00",
                },
                {
                    "type": "EvaluatorVerdict",
                    "pr_id": "42",
                    "approved": True,
                    "at": "2026-10-15T00:00:03+00:00",
                },
                {
                    "type": "StageFinished",
                    "stage": "classify",
                    "skipped": False,
                    "at": "2026-10-15T00:00:04+00:00",
                },
                {"type": "StageStarted", "stage": "classify", "at": "2026-10-15T00:01:00+00:00"},
                {
                    "type": "ClassifyProgress",
                    "kind": "pull",
                    "index": 15,
                    "total": 200,
                    "at": "2026-10-15T00:01:01+00:00",
                },
                {
                    "type": "ClassificationAttempt",
                    "pr_id": "9",
                    "at": "2026-10-15T00:01:02+00:00",
                },
                {
                    "type": "StageFinished",
                    "stage": "classify",
                    "skipped": False,
                    "at": "2026-10-15T00:01:03+00:00",
                },
            ]
        ),
        encoding="utf-8",
    )
    console, stream = _console()

    show_logs(tmp_path, console=console)

    lines = stream.getvalue().splitlines()
    batch = next(line for line in lines if "2/12" in line and "classified" not in line)
    classified = next(line for line in lines if "classified" in line and "42" in line)
    approved = next(line for line in lines if "approved" in line)
    pull = next(line for line in lines if "15/200" in line and "classified" not in line)
    later = next(line for line in lines if "classified" in line and "9" in line)
    assert "batch" in batch
    assert "batch 2/12" in classified
    assert "batch 2/12" in approved
    assert "pr" in pull
    assert "pr 15/200" in later
    assert lines.index(batch) < lines.index(classified)
    assert lines.index(pull) < lines.index(later)


def test_logs_infer_batch_and_pull_place_without_progress_events(tmp_path: Path) -> None:
    pulls = [_normalized(pr_id) for pr_id in ("1", "2", "3")]
    normalize = tmp_path / "normalize"
    normalize.mkdir()
    (normalize / "output.json").write_text(json.dumps(pulls), encoding="utf-8")
    (tmp_path / "config.json").write_text(json.dumps({"batch_size": 1}), encoding="utf-8")
    (tmp_path / "events.jsonl").write_text(
        _dump(
            [
                {"type": "StageStarted", "stage": "classify"},
                {"type": "ClassificationAttempt", "pr_id": "2"},
                {"type": "StageFinished", "stage": "classify", "skipped": False},
            ]
        ),
        encoding="utf-8",
    )
    console, stream = _console()
    show_logs(tmp_path, console=console)
    assert "pr 2/3" in stream.getvalue()

    (tmp_path / "config.json").write_text(json.dumps({"batch_size": 2}), encoding="utf-8")
    (tmp_path / "events.jsonl").write_text(
        _dump(
            [
                {"type": "StageStarted", "stage": "classify"},
                {"type": "ClassificationAttempt", "pr_id": "3"},
                {"type": "StageFinished", "stage": "classify", "skipped": False},
            ]
        ),
        encoding="utf-8",
    )
    console, stream = _console()
    show_logs(tmp_path, console=console)
    assert "batch 2/2" in stream.getvalue()


def _normalized(pr_id: str) -> dict[str, object]:
    return {
        "schema_version": "1",
        "context": {
            "repository": "acme/app",
            "pr_id": pr_id,
            "title": "Fix",
            "state": "open",
            "is_merged": False,
        },
        "description": "note",
        "general_discussion": [],
        "code_review_threads": [],
    }


def test_logs_follow_prints_a_line_appended_after_the_first_read(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text(
        json.dumps({"type": "StageStarted", "stage": "mine", "at": "2026-10-15T00:00:00+00:00"})
        + "\n",
        encoding="utf-8",
    )
    console, stream = _console()
    calls = {"n": 0}

    def pause(_seconds: float) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    json.dumps(
                        {
                            "type": "ClassificationAttempt",
                            "pr_id": "99",
                            "at": "2026-10-15T00:00:05+00:00",
                        }
                    )
                    + "\n"
                )
            return
        raise KeyboardInterrupt

    show_logs(tmp_path, follow=True, console=console, pause=pause)

    text = stream.getvalue()
    assert "mine" in text
    assert "classified" in text
    assert "99" in text
    assert calls["n"] == 2
