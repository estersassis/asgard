"""On-disk run discovery and UI reopen."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from asgard.ui.app import create_app
from asgard.ui.runs import discover_runs, resolve_run_dir, stable_run_id
from asgard.ui.session import RunSession


def _write_run(root: Path, repo_dir: str, stamp: str, *, repo: str, complete: bool) -> Path:
    run_dir = root / repo_dir / stamp
    run_dir.mkdir(parents=True)
    (run_dir / "config.json").write_text(
        json.dumps({"repo": repo, "output_dir": str(run_dir)}),
        encoding="utf-8",
    )
    (run_dir / "events.jsonl").write_text(
        json.dumps({"type": "StageFinished", "stage": "bundle", "skipped": False}) + "\n",
        encoding="utf-8",
    )
    if complete:
        (run_dir / "SECURITY.md").write_text("# Security\n", encoding="utf-8")
    return run_dir


def test_stable_run_id_and_resolve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs = tmp_path / "runs"
    run_dir = _write_run(runs, "acme__app", "20261016T120000Z", repo="acme/app", complete=True)
    monkeypatch.chdir(tmp_path)

    run_id = stable_run_id(run_dir)
    assert run_id == "acme__app--20261016T120000Z"
    assert resolve_run_dir(run_id) == run_dir.resolve()
    assert resolve_run_dir("missing--id") is None
    assert resolve_run_dir("../evil--stamp") is None


def test_discover_runs_includes_tmp_runs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs = tmp_path / "runs"
    tmp_runs = tmp_path / "tmp" / "runs"
    _write_run(runs, "acme__app", "20261016T120000Z", repo="acme/app", complete=True)
    _write_run(tmp_runs, "beta__lib", "20261016T130000Z", repo="beta/lib", complete=False)
    monkeypatch.chdir(tmp_path)

    found = discover_runs()
    ids = [item["id"] for item in found]
    assert "beta__lib--20261016T130000Z" in ids
    assert "acme__app--20261016T120000Z" in ids
    by_id = {item["id"]: item for item in found}
    assert by_id["acme__app--20261016T120000Z"]["complete"] is True
    assert by_id["beta__lib--20261016T130000Z"]["complete"] is False


def test_api_lists_and_opens_disk_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    runs = tmp_path / "runs"
    run_dir = _write_run(
        runs,
        "acme__app",
        "20261016T120000Z",
        repo="acme/app",
        complete=True,
    )
    monkeypatch.chdir(tmp_path)
    client = TestClient(create_app(dotenv=None))

    listed = client.get("/api/runs")
    assert listed.status_code == 200
    body = listed.json()
    assert body[0]["id"] == "acme__app--20261016T120000Z"
    assert body[0]["repo"] == "acme/app"
    assert body[0]["stamp"] == "20261016T120000Z"

    status = client.get("/api/runs/acme__app--20261016T120000Z")
    assert status.status_code == 200
    assert status.json()["running"] is False
    assert Path(status.json()["output_dir"]).resolve() == run_dir.resolve()

    log = client.get("/api/runs/acme__app--20261016T120000Z/log")
    assert log.status_code == 200
    assert "StageFinished" in log.text

    md = client.get("/api/runs/acme__app--20261016T120000Z/SECURITY.md")
    assert md.status_code == 200
    assert "Security" in md.text


def test_retry_classify_drops_errors_and_resumes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = tmp_path / "runs"
    run_dir = _write_run(
        runs,
        "acme__app",
        "20261016T140000Z",
        repo="acme/app",
        complete=False,
    )
    classify = run_dir / "classify"
    classify.mkdir()
    (classify / "items.jsonl").write_text(
        "".join(
            json.dumps({"id": pr_id, "payload": {"pr_id": pr_id, "status": status}}) + "\n"
            for pr_id, status in (("1", "approved"), ("2", "error"), ("3", "error"))
        ),
        encoding="utf-8",
    )
    (classify / "output.json").write_text("[]\n", encoding="utf-8")
    (run_dir / "consolidate").mkdir()
    (run_dir / "consolidate" / "output.json").write_text("[]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    session = RunSession.open_existing(run_dir, session_id="acme__app--20261016T140000Z")
    started: list[str | None] = []

    def fake_start(**kwargs: object) -> Path:
        started.append(kwargs.get("start"))  # type: ignore[arg-type]
        return run_dir

    monkeypatch.setattr(session, "start", fake_start)
    output_dir, removed = session.retry_classify(pr_ids=["2"])
    assert output_dir == run_dir
    assert removed == ["2"]
    assert started == ["classify"]
    kept = (classify / "items.jsonl").read_text(encoding="utf-8")
    assert '"id": "1"' in kept
    assert '"id": "2"' not in kept
    assert '"id": "3"' in kept
    assert not (classify / "output.json").exists()
    assert not (run_dir / "consolidate").exists()

    client = TestClient(create_app(dotenv=None))
    # Re-seed remaining error for the HTTP path.
    (classify / "items.jsonl").write_text(
        "".join(
            json.dumps({"id": pr_id, "payload": {"pr_id": pr_id, "status": status}}) + "\n"
            for pr_id, status in (("1", "approved"), ("3", "error"))
        ),
        encoding="utf-8",
    )
    (classify / "output.json").write_text("[]\n", encoding="utf-8")

    # Opening the run through the API store, then retry all errors.
    assert client.get("/api/runs/acme__app--20261016T140000Z").status_code == 200
    store = client.app.state.store
    live = store.get("acme__app--20261016T140000Z")
    assert live is not None
    monkeypatch.setattr(live, "start", fake_start)
    started.clear()
    response = client.post(
        "/api/runs/acme__app--20261016T140000Z/classify/retry",
        json={"all_errors": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["pr_ids"] == ["3"]
    assert started == ["classify"]

    missing = client.post(
        "/api/runs/acme__app--20261016T140000Z/classify/retry",
        json={"pr_ids": ["1"]},
    )
    assert missing.status_code == 400
    assert "error or unresolved" in missing.json()["detail"]


def test_retry_classify_allows_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runs = tmp_path / "runs"
    run_dir = _write_run(
        runs,
        "acme__app",
        "20261016T150000Z",
        repo="acme/app",
        complete=False,
    )
    classify = run_dir / "classify"
    classify.mkdir()
    (classify / "items.jsonl").write_text(
        "".join(
            json.dumps({"id": pr_id, "payload": {"pr_id": pr_id, "status": status}})
            + "\n"
            for pr_id, status in (
                ("1", "approved"),
                ("2", "unresolved_no_progress"),
                ("3", "unresolved_max_iter"),
            )
        ),
        encoding="utf-8",
    )
    (classify / "output.json").write_text("[]\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    session = RunSession.open_existing(run_dir, session_id="acme__app--20261016T150000Z")
    started: list[str | None] = []

    def fake_start(**kwargs: object) -> Path:
        started.append(kwargs.get("start"))  # type: ignore[arg-type]
        return run_dir

    monkeypatch.setattr(session, "start", fake_start)
    output_dir, removed = session.retry_classify(pr_ids=["2"])
    assert output_dir == run_dir
    assert removed == ["2"]
    assert started == ["classify"]
    kept = (classify / "items.jsonl").read_text(encoding="utf-8")
    assert '"id": "1"' in kept
    assert '"id": "2"' not in kept
    assert '"id": "3"' in kept

    (classify / "items.jsonl").write_text(
        "".join(
            json.dumps({"id": pr_id, "payload": {"pr_id": pr_id, "status": status}})
            + "\n"
            for pr_id, status in (
                ("1", "approved"),
                ("3", "unresolved_max_iter"),
                ("4", "error"),
            )
        ),
        encoding="utf-8",
    )
    started.clear()
    _, removed_all = session.retry_classify(all_errors=True)
    assert set(removed_all) == {"3", "4"}
    assert started == ["classify"]
