"""Consolidate stage: synthesis input, audit, and summary. No network."""

import json
from pathlib import Path

import pytest

from asgard.core.config import ConfigError
from asgard.core.stage import RunContext
from asgard.core.stages.consolidate import ConsolidateStage

_TRACE = "classify/trace.jsonl"


def _classification(security: bool, rationale: str) -> dict[str, object]:
    evidence: list[dict[str, str]] = []
    if security:
        evidence = [{"quote": "authentication bypass", "location": "description"}]
    return {
        "has_security_discussion": security,
        "evidence": evidence,
        "rationale": rationale,
    }


def _record(
    pr_id: str,
    status: str,
    *,
    iterations: int = 1,
    classification: dict[str, object] | None,
) -> dict[str, object]:
    return {
        "pr_id": pr_id,
        "status": status,
        "iterations": iterations,
        "classification": classification,
        "evaluation": None,
    }


def _classified() -> list[dict[str, object]]:
    return [
        _record("1", "approved", classification=_classification(True, "token check")),
        _record(
            "2",
            "approved",
            iterations=2,
            classification=_classification(False, "ui only"),
        ),
        _record(
            "3",
            "unresolved_max_iter",
            iterations=3,
            classification=_classification(True, "still security"),
        ),
        _record(
            "4",
            "error",
            iterations=2,
            classification=_classification(True, "failed after"),
        ),
        _record("5", "error", classification=None),
        _record(
            "6",
            "unresolved_no_progress",
            iterations=2,
            classification=_classification(True, "same answer"),
        ),
    ]


def _prepare(tmp_path: Path, mine: list[int] | None = None) -> None:
    titles = {
        "1": "Fix authentication bypass",
        "2": "Rename the button",
        "3": "Harden token parsing",
        "4": "Reject expired sessions",
        "5": "Empty failure",
        "6": "Repeat the warning",
    }
    documents = [
        {
            "context": {
                "repository": "acme/app",
                "pr_id": pr_id,
                "title": title,
            }
        }
        for pr_id, title in titles.items()
    ]
    normalize = tmp_path / "normalize"
    normalize.mkdir()
    (normalize / "output.json").write_text(json.dumps(documents), encoding="utf-8")
    classify = tmp_path / "classify"
    classify.mkdir()
    lines = [
        {"pr_id": "1", "role": "classifier"},
        {"pr_id": "1", "role": "evaluator"},
        {"pr_id": "2", "role": "classifier"},
        {"pr_id": "3", "role": "classifier"},
        {"pr_id": "4", "role": "classifier"},
        {"pr_id": "5", "role": "classifier"},
        {"pr_id": "6", "role": "classifier"},
    ]
    body = "".join(json.dumps(line) + "\n" for line in lines)
    (classify / "trace.jsonl").write_text(body, encoding="utf-8")
    if mine is not None:
        mined = tmp_path / "mine"
        mined.mkdir()
        (mined / "output.json").write_text(json.dumps(mine), encoding="utf-8")


def _run(
    tmp_path: Path,
    data: object,
    *,
    include_unresolved: bool = False,
) -> list[dict[str, object]]:
    stage = ConsolidateStage(include_unresolved)
    return stage.run(RunContext(tmp_path, lambda _event: None), data)


def _audit(tmp_path: Path) -> list[dict[str, object]]:
    text = (tmp_path / "consolidate" / "audit.jsonl").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line]


def test_only_approved_security_pulls_enter_synthesis(tmp_path: Path) -> None:
    _prepare(tmp_path, [1, 2, 3, 4, 5, 6])
    result = _run(tmp_path, _classified())
    saved = json.loads((tmp_path / "consolidate" / "output.json").read_text(encoding="utf-8"))
    assert saved == result
    assert result == [
        {
            "pr_id": "1",
            "title": "Fix authentication bypass",
            "link": "https://github.com/acme/app/pull/1",
            "evidence": [{"quote": "authentication bypass", "location": "description"}],
            "rationale": "token check",
        }
    ]


def test_include_unresolved_keeps_failed_security_classifications(tmp_path: Path) -> None:
    _prepare(tmp_path, [1, 2, 3, 4, 5, 6])
    result = _run(tmp_path, _classified(), include_unresolved=True)
    assert [item["pr_id"] for item in result] == ["1", "3", "4", "6"]
    assert [item["rationale"] for item in result] == [
        "token check",
        "still security",
        "failed after",
        "same answer",
    ]


def test_audit_has_one_record_per_pull_with_trace_lines(tmp_path: Path) -> None:
    _prepare(tmp_path, [1, 2, 3, 4, 5, 6])
    _run(tmp_path, _classified())
    audit = _audit(tmp_path)
    assert [item["pr_id"] for item in audit] == ["1", "2", "3", "4", "5", "6"]
    assert audit[0] == {
        "pr_id": "1",
        "decision": True,
        "status": "approved",
        "iterations": 1,
        "trace": _TRACE,
        "trace_lines": [1, 2],
    }
    assert audit[1]["decision"] is False
    assert audit[1]["trace_lines"] == [3]
    assert audit[4]["decision"] is None
    assert audit[4]["status"] == "error"
    assert audit[4]["trace_lines"] == [6]
    assert all(item["trace"] == _TRACE for item in audit)


def test_summary_counts_add_up(tmp_path: Path) -> None:
    _prepare(tmp_path, [1, 2, 3, 4, 5, 6])
    _run(tmp_path, _classified())
    summary = json.loads((tmp_path / "consolidate" / "summary.json").read_text(encoding="utf-8"))
    assert summary == {"total": 6, "security": 1, "non_security": 1, "unresolved": 4}
    assert summary["security"] + summary["non_security"] + summary["unresolved"] == summary["total"]


def test_node_id_resolves_to_the_pull_number(tmp_path: Path) -> None:
    _prepare(tmp_path, [7])
    node_id = "PR_kwDOABV8os8AAAABH4lFNw"
    mined = tmp_path / "mine" / "prs"
    mined.mkdir()
    (mined / "7.json").write_text(json.dumps({"node_id": node_id, "number": 7}), encoding="utf-8")
    document = {
        "context": {
            "repository": "acme/app",
            "pr_id": node_id,
            "title": "Stop padding inputs",
        }
    }
    (tmp_path / "normalize" / "output.json").write_text(json.dumps([document]), encoding="utf-8")
    trace = json.dumps({"pr_id": node_id, "role": "classifier"}) + "\n"
    (tmp_path / "classify" / "trace.jsonl").write_text(trace, encoding="utf-8")
    (tmp_path / "mine" / "output.json").write_text(json.dumps([7]), encoding="utf-8")

    result = _run(
        tmp_path,
        [_record(node_id, "approved", classification=_classification(True, "padding"))],
    )

    assert result == [
        {
            "pr_id": "7",
            "title": "Stop padding inputs",
            "link": "https://github.com/acme/app/pull/7",
            "evidence": [{"quote": "authentication bypass", "location": "description"}],
            "rationale": "padding",
        }
    ]
    assert _audit(tmp_path) == [
        {
            "pr_id": "7",
            "decision": True,
            "status": "approved",
            "iterations": 1,
            "trace": _TRACE,
            "trace_lines": [1],
        }
    ]


def test_mine_set_and_malformed_input_raise(tmp_path: Path) -> None:
    _prepare(tmp_path, [1])
    approved = _record("1", "approved", classification=_classification(True, "token check"))

    with pytest.raises(ConfigError, match="do not match the mined set"):
        _run(tmp_path, [_record("9", "approved", classification=_classification(True, "other"))])
    with pytest.raises(ConfigError, match="not a list of numbers"):
        (tmp_path / "mine" / "output.json").write_text(json.dumps(["1"]), encoding="utf-8")
        _run(tmp_path, [approved])

    (tmp_path / "mine" / "output.json").unlink()
    with pytest.raises(ConfigError, match="normalized pull 9 is missing"):
        _run(tmp_path, [_record("9", "approved", classification=_classification(True, "other"))])
    with pytest.raises(ConfigError, match="duplicate pull request 1"):
        _run(tmp_path, [approved, approved])
    with pytest.raises(ConfigError, match="requires classified pull requests"):
        _run(tmp_path, {"pr_id": "1"})
    broken = _record("1", "approved", classification={"has_security_discussion": "yes"})
    with pytest.raises(ConfigError, match="requires classified pull requests"):
        _run(tmp_path, [broken])
