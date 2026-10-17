"""On-disk run discovery and UI reopen."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from asgard.ui.app import create_app
from asgard.ui.runs import discover_runs, resolve_run_dir, stable_run_id


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
