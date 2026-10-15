"""Optional security-signal filter between normalize and classify."""

import json
from pathlib import Path

from asgard.core.events import PRFiltered, StageFinished, StageStarted
from asgard.core.stage import RunContext
from asgard.core.stages.prefilter import PrefilterStage

_REASON = "no security keyword, label, or sensitive path"


def _pull(
    pr_id: str,
    *,
    title: str = "Rename the helper",
    description: str = "Small cleanup.",
    discussion: list[str] | None = None,
    comments: list[str] | None = None,
) -> dict[str, object]:
    threads: list[dict[str, object]] = []
    if comments is not None:
        threads.append({"scope": "FILE:src/app.py", "comments": comments})
    return {
        "schema_version": "1",
        "context": {
            "repository": "acme/app",
            "pr_id": pr_id,
            "title": title,
            "state": "open",
            "is_merged": False,
        },
        "description": description,
        "general_discussion": discussion or [],
        "code_review_threads": threads,
    }


def _mine(root: Path, pr_id: str, payload: dict[str, object]) -> None:
    path = root / "mine" / "prs" / f"{pr_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _run(
    root: Path,
    pulls: list[dict[str, object]],
    *,
    prefilter: bool | None = None,
) -> tuple[list[dict[str, object]], list[object]]:
    events: list[object] = []
    result = PrefilterStage(prefilter).run(RunContext(root, events.append), pulls)
    assert isinstance(result, list)
    return result, events


def _removed(root: Path) -> dict[str, object]:
    payload = json.loads((root / "prefilter" / "removed.json").read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return payload


def test_disabled_filter_keeps_every_pull(tmp_path: Path) -> None:
    pulls = [_pull("1"), _pull("2", description="Rename a helper.")]

    result, events = _run(tmp_path, pulls, prefilter=False)

    assert result == pulls
    assert events == [
        StageStarted(stage="prefilter", total=2),
        StageFinished(stage="prefilter"),
    ]
    assert _removed(tmp_path) == {"before": 2, "after": 2, "removed": []}
    saved = json.loads((tmp_path / "prefilter" / "output.json").read_text(encoding="utf-8"))
    assert saved == pulls


def test_enabled_filter_records_the_reason_and_the_input_total(tmp_path: Path) -> None:
    quiet = _pull("1")
    kept = _pull("2", title="Store the token")
    pulls = [quiet, kept]

    result, events = _run(tmp_path, pulls, prefilter=True)

    assert [item["context"]["pr_id"] for item in result] == ["2"]
    assert events == [
        StageStarted(stage="prefilter", total=2),
        PRFiltered(pr_id="1", reason=_REASON),
        StageFinished(stage="prefilter"),
    ]
    removed = _removed(tmp_path)
    assert removed == {
        "before": 2,
        "after": 1,
        "removed": [{"pr_id": "1", "reason": _REASON}],
    }
    started = events[0]
    assert isinstance(started, StageStarted)
    filtered = [event for event in events if isinstance(event, PRFiltered)]
    assert removed["before"] == started.total
    assert removed["after"] == started.total - len(filtered)


def test_config_enables_the_filter(tmp_path: Path) -> None:
    (tmp_path / "config.json").write_text(json.dumps({"prefilter": True}), encoding="utf-8")

    result, events = _run(tmp_path, [_pull("7"), _pull("8", title="Fix XSS")])

    assert [item["context"]["pr_id"] for item in result] == ["8"]
    assert events[0] == StageStarted(stage="prefilter", total=2)
    assert _removed(tmp_path)["before"] == 2
    assert _removed(tmp_path)["after"] == 1


def test_text_label_or_path_keeps_the_pull(tmp_path: Path) -> None:
    pulls = [
        _pull("title", title="CVE-2024-1"),
        _pull("description", description="Check the password hash."),
        _pull("discussion", discussion=["This looks like XSS."]),
        _pull("review", comments=["Please sanitiz the input."]),
        _pull("label-name"),
        _pull("label-string"),
        _pull("filename"),
        _pull("file-string"),
        _pull("thread-path"),
        _pull("comment-path"),
    ]
    _mine(tmp_path, "label-name", {"labels": [{"name": "Security"}]})
    _mine(tmp_path, "label-string", {"labels": ["needs-encryption"]})
    _mine(tmp_path, "filename", {"files": [{"filename": "src/oauth/client.py"}]})
    _mine(tmp_path, "file-string", {"files": ["internal/session.go"]})
    _mine(tmp_path, "thread-path", {"threads": [{"path": "lib/auth.py", "comments": []}]})
    _mine(
        tmp_path,
        "comment-path",
        {"threads": [{"comments": [{"path": "pkg/credential.go"}]}]},
    )

    result, events = _run(tmp_path, pulls, prefilter=True)

    kept_ids = [item["context"]["pr_id"] for item in result]
    expected_ids = [pull["context"]["pr_id"] for pull in pulls]
    assert kept_ids == expected_ids
    assert not any(isinstance(event, PRFiltered) for event in events)
    assert _removed(tmp_path) == {"before": len(pulls), "after": len(pulls), "removed": []}


def test_missing_path_does_not_remove_on_its_own(tmp_path: Path) -> None:
    matched = _pull("1", title="Reset the password")
    quiet = _pull("2")
    _mine(tmp_path, "1", {"number": 1})
    _mine(tmp_path, "2", {"labels": ["docs"]})

    result, events = _run(tmp_path, [matched, quiet], prefilter=True)

    assert [item["context"]["pr_id"] for item in result] == ["1"]
    assert events[1] == PRFiltered(pr_id="2", reason=_REASON)
    assert _removed(tmp_path)["removed"] == [{"pr_id": "2", "reason": _REASON}]
