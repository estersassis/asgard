"""CLI controls: one stage, export, log modes, and exit codes. No network."""

import json
import urllib.request
from pathlib import Path

import pytest

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
