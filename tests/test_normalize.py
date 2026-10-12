"""GraphQL dumps and mined REST pulls normalize to the same document."""

import json
from pathlib import Path

import pytest

from asgard.core.events import Error, StageFinished, StageStarted
from asgard.core.normalization import NormalizedPullRequest, format_pull
from asgard.core.orchestrator import Orchestrator
from asgard.core.stages.normalize import NormalizeStage

NODE_ID = "MDExOlB1bGxSZXF1ZXN0MjA0NjQ2NTU="
DESCRIPTION = "See https://code.djangoproject.com/ticket/23381#ticket\n"
TIMEZONE = "django/utils/timezone.py"
TRANSLATION = "django/utils/translation/__init__.py"

USEFUL = (
    "I would find it useful to write two tests targeted specifically at this issue, "
    "one for each decorator, in tests/utils_tests/test_timezone.py and "
    "tests/i18n/tests.py respectively.\n"
)
BUILDBOT = "buildbot, test this please.\n"
UPDATED = (
    "@aaugustin I updated my PR following your comments. Regarding `timezone.override`, "
    "it is not a decorator yet (I was working on a PR for that when I run into this bug... "
    "but I will add some tests for that case in my PR)\n"
)
MERGED_NOTE = (
    "Merged manually in efcbf3e095dce3491eb52774978afe3f7bbdf217 with minor edits, merci.\n"
)
WHY_NOT = "Why not remove that line entirely?\n"
ATTRIBUTES = (
    'Well, "some people" consider that attributes should be defined at initialization, '
    "which is arguable. If this is not a rule within django codebase, I have no objection "
    "to remove those lines.\n"
)
SAME = "Same question as above.\n"


def test_dump_and_rest_normalize_reduced_pr_3135() -> None:
    dump = _dump_3135()
    rest = _rest_3135()

    from_dump = format_pull(dump)
    from_rest = format_pull(rest, repo="other/repo")

    assert from_dump == from_rest
    assert from_dump == {
        "schema_version": "1",
        "context": {
            "repository": "django/django",
            "pr_id": NODE_ID,
            "title": "Fix override decorator",
            "state": "CLOSED",
            "is_merged": False,
        },
        "description": DESCRIPTION,
        "general_discussion": [
            USEFUL.strip(),
            UPDATED.strip(),
            BUILDBOT.strip(),
            MERGED_NOTE.strip(),
        ],
        "code_review_threads": [
            {
                "scope": f"FILE:{TIMEZONE}",
                "comments": [WHY_NOT.strip(), ATTRIBUTES.strip()],
            },
            {
                "scope": f"FILE:{TRANSLATION}",
                "comments": [SAME.strip()],
            },
        ],
    }
    assert "ship it from the review body" not in str(from_rest)
    NormalizedPullRequest.model_validate(from_dump)


def test_long_body_is_kept_character_for_character() -> None:
    body = ("line\n" * 20_000) + "  tail stays  \n"
    comment = "x" * 50_000
    dump = {
        "base_repository": "django/django",
        "id": NODE_ID,
        "title": "long",
        "state": "OPEN",
        "merged": False,
        "body": body,
        "timeline_items": [
            {"__typename": "IssueComment", "body": comment},
        ],
    }
    rest = {
        "node_id": NODE_ID,
        "title": "long",
        "state": "open",
        "merged": False,
        "body": body,
        "base": {"repo": {"full_name": "django/django"}},
        "issue_comments": [{"body": comment, "created_at": "2014-08-28T20:48:15Z"}],
        "reviews": [{"body": "y" * 10_000}],
        "threads": [],
    }

    from_dump = format_pull(dump)
    from_rest = format_pull(rest)
    assert from_dump["description"] == body
    assert from_dump["general_discussion"] == [comment]
    assert from_rest["description"] == body
    assert from_rest["general_discussion"] == [comment]
    NormalizedPullRequest.model_validate(from_dump)
    NormalizedPullRequest.model_validate(from_rest)


def test_null_bodies_become_empty_and_blank_comments_drop() -> None:
    formatted = format_pull(
        {
            "title": None,
            "body": None,
            "state": None,
            "timeline_items": [
                {"__typename": "IssueComment", "body": None},
                {"__typename": "IssueComment", "body": "   \n"},
                {"__typename": "IssueComment", "body": " kept "},
                {
                    "__typename": "PullRequestReviewThread",
                    "path": "a.py",
                    "comments": [{"body": None}, {"body": "  "}],
                },
                {
                    "__typename": "PullRequestReviewThread",
                    "path": None,
                    "comments": [{"body": " file note ", "line": None, "start_line": None}],
                },
            ],
        }
    )

    assert formatted["description"] == ""
    assert formatted["context"]["title"] == ""
    assert formatted["context"]["pr_id"] == ""
    assert formatted["context"]["repository"] == "unknown"
    assert formatted["general_discussion"] == ["kept"]
    assert formatted["code_review_threads"] == [
        {"scope": "FILE:unknown_file", "comments": ["file note"]},
    ]


def test_thread_scope_uses_line_then_start_line() -> None:
    formatted = format_pull(
        {
            "base_repository": "django/django",
            "id": "n",
            "title": "lines",
            "state": "open",
            "merged": False,
            "body": "",
            "timeline_items": [
                {
                    "__typename": "PullRequestReviewThread",
                    "path": "test.py",
                    "comments": [{"body": "on the line", "line": 42}],
                },
                {
                    "__typename": "PullRequestReviewThread",
                    "path": "test.py",
                    "comments": [{"body": "on the range", "start_line": 10}],
                },
                {
                    "__typename": "HeadRefForcePushedEvent",
                },
            ],
        }
    )

    assert [thread["scope"] for thread in formatted["code_review_threads"]] == [
        "FILE:test.py#L42",
        "FILE:test.py#L10",
    ]
    assert formatted["context"]["state"] == "OPEN"


def test_rest_merged_state_is_canonical_and_repo_falls_back() -> None:
    formatted = format_pull(
        {
            "node_id": NODE_ID,
            "title": "landed",
            "state": "closed",
            "merged": True,
            "body": "done",
            "issue_comments": [
                {"body": "later", "created_at": "2014-08-28T21:00:00Z"},
                {"body": "earlier", "created_at": "2014-08-28T20:00:00Z"},
            ],
            "reviews": [{"body": "ship it from the review body", "state": "APPROVED"}],
            "threads": [
                {
                    "id": 2,
                    "comments": [
                        {
                            "body": "after",
                            "path": "b.py",
                            "line": 3,
                            "created_at": "2014-08-28T22:00:00Z",
                        }
                    ],
                },
                {
                    "id": 1,
                    "comments": [
                        {
                            "body": "reply",
                            "path": "a.py",
                            "line": None,
                            "start_line": 8,
                            "created_at": "2014-08-28T20:30:00Z",
                        },
                        {
                            "body": "root",
                            "path": "a.py",
                            "created_at": "2014-08-28T20:10:00Z",
                        },
                    ],
                },
            ],
        },
        repo="django/django",
    )

    assert formatted["context"]["repository"] == "django/django"
    assert formatted["context"]["pr_id"] == NODE_ID
    assert formatted["context"]["state"] == "MERGED"
    assert formatted["context"]["is_merged"] is True
    assert formatted["general_discussion"] == ["earlier", "later"]
    assert formatted["code_review_threads"] == [
        {"scope": "FILE:a.py#L8", "comments": ["reply", "root"]},
        {"scope": "FILE:b.py#L3", "comments": ["after"]},
    ]
    assert "ship it from the review body" not in formatted["description"]
    assert "ship it from the review body" not in formatted["general_discussion"]
    NormalizedPullRequest.model_validate(formatted)


def test_dump_timeline_order_is_preserved() -> None:
    formatted = format_pull(
        {
            "id": "keep-order",
            "state": "MERGED",
            "merged": True,
            "timeline_items": [
                {
                    "__typename": "IssueComment",
                    "body": "listed first",
                    "created_at": "2014-08-28T22:00:00Z",
                },
                {
                    "__typename": "IssueComment",
                    "body": "listed second",
                    "created_at": "2014-08-28T20:00:00Z",
                },
            ],
        }
    )

    assert formatted["general_discussion"] == ["listed first", "listed second"]
    assert formatted["context"]["state"] == "MERGED"
    NormalizedPullRequest.model_validate(formatted)


def test_published_schema_matches_the_model() -> None:
    path = (
        Path(__file__).resolve().parents[1] / "asgard" / "schemas" / "normalized_pr.schema.json"
    )
    published = json.loads(path.read_text(encoding="utf-8"))
    assert published == NormalizedPullRequest.model_json_schema()
    documents = (
        format_pull(_dump_3135()),
        format_pull(_rest_3135()),
        format_pull(_rest_note(9)),
    )
    for document in documents:
        NormalizedPullRequest.model_validate(document)


def test_normalize_stage_writes_output_and_events(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    numbers = [3135, 2046, 9]
    _write_mined(
        tmp_path,
        {3135: _rest_3135(), 2046: _rest_note(2046, merged=True), 9: _rest_note(9)},
    )
    calls: list[str] = []
    real = format_pull

    def spy(payload: dict, *, repo: str | None = None) -> dict:
        calls.append(str(payload.get("node_id")))
        return real(payload, repo=repo)

    monkeypatch.setattr("asgard.core.stages.normalize.format_pull", spy)
    sink = _Sink()
    result = Orchestrator([NormalizeStage(repo="django/django")], [sink]).run(
        tmp_path,
        data=numbers,
    )

    assert calls == [NODE_ID, "node-2046", "node-9"]
    assert [type(event) for event in sink.events] == [StageStarted, StageFinished]
    assert sink.events[0] == StageStarted(stage="normalize")
    assert sink.events[1] == StageFinished(stage="normalize")
    saved = json.loads((tmp_path / "normalize" / "output.json").read_text(encoding="utf-8"))
    assert saved == result
    assert result[0] == real(_rest_3135(), repo="django/django")
    assert result[1]["context"]["state"] == "MERGED"
    assert result[1]["description"] == "body 2046"
    assert result[2]["general_discussion"] == ["note 9"]
    for document in result:
        NormalizedPullRequest.model_validate(document)

    calls.clear()
    sink.events.clear()
    again = Orchestrator([NormalizeStage(repo="django/django")], [sink]).run(tmp_path, data=numbers)
    assert again == result
    assert calls == []
    assert sink.events == [StageFinished(stage="normalize", skipped=True)]


def test_normalize_stage_does_not_reformat_saved_ids(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    numbers = [3135, 40, 41]
    _write_mined(
        tmp_path,
        {3135: _rest_3135(), 40: _rest_note(40), 41: _rest_note(41, merged=True)},
    )
    calls: list[str] = []
    real = format_pull

    def fail_on_second(payload: dict, *, repo: str | None = None) -> dict:
        node = str(payload.get("node_id"))
        calls.append(node)
        if node == "node-40":
            raise RuntimeError("stopped")
        return real(payload, repo=repo)

    monkeypatch.setattr("asgard.core.stages.normalize.format_pull", fail_on_second)
    sink = _Sink()
    with pytest.raises(RuntimeError, match="stopped"):
        Orchestrator([NormalizeStage()], [sink]).run(tmp_path, data=numbers)
    assert calls == [NODE_ID, "node-40"]
    assert not (tmp_path / "normalize" / "output.json").exists()
    assert any(isinstance(event, Error) and event.stage == "normalize" for event in sink.events)

    calls.clear()

    def counting(payload: dict, *, repo: str | None = None) -> dict:
        calls.append(str(payload.get("node_id")))
        return real(payload, repo=repo)

    monkeypatch.setattr("asgard.core.stages.normalize.format_pull", counting)
    sink.events.clear()
    result = Orchestrator([NormalizeStage()], [sink]).run(tmp_path, data=numbers)
    assert calls == ["node-40", "node-41"]
    assert [type(event) for event in sink.events] == [StageStarted, StageFinished]
    assert result[0]["context"]["pr_id"] == NODE_ID
    assert result[1]["context"]["pr_id"] == "node-40"
    assert result[2]["context"]["state"] == "MERGED"
    saved = json.loads((tmp_path / "normalize" / "output.json").read_text(encoding="utf-8"))
    assert saved == result


class _Sink:
    def __init__(self) -> None:
        self.events: list[object] = []

    def handle(self, event: object) -> None:
        self.events.append(event)


def _write_mined(directory: Path, pulls: dict[int, dict]) -> None:
    folder = directory / "mine" / "prs"
    folder.mkdir(parents=True)
    for number, payload in pulls.items():
        (folder / f"{number}.json").write_text(json.dumps(payload), encoding="utf-8")


def _rest_note(number: int, *, merged: bool = False) -> dict:
    return {
        "node_id": f"node-{number}",
        "title": f"pull {number}",
        "state": "closed" if merged else "open",
        "merged": merged,
        "body": f"body {number}",
        "base": {"repo": {"full_name": "django/django"}},
        "issue_comments": [
            {"body": f"note {number}", "created_at": "2014-09-01T00:00:00Z"},
        ],
        "reviews": [{"body": "review body stays out"}],
        "threads": [],
    }


def _dump_3135() -> dict:
    return {
        "id": NODE_ID,
        "base_repository": "django/django",
        "title": "Fix override decorator",
        "state": "CLOSED",
        "merged": False,
        "body": DESCRIPTION,
        "timeline_items": [
            {
                "__typename": "PullRequestReviewThread",
                "path": TIMEZONE,
                "comments": [
                    {"body": WHY_NOT, "created_at": {"$date": "2014-08-28T20:43:52.000Z"}},
                    {"body": ATTRIBUTES, "created_at": {"$date": "2014-08-28T20:47:36.000Z"}},
                ],
            },
            {
                "__typename": "PullRequestReviewThread",
                "path": TRANSLATION,
                "comments": [
                    {"body": SAME, "created_at": {"$date": "2014-08-28T20:44:04.000Z"}},
                ],
            },
            {
                "__typename": "IssueComment",
                "body": USEFUL,
                "created_at": {"$date": "2014-08-28T20:48:15.000Z"},
            },
            {"__typename": "HeadRefForcePushedEvent"},
            {
                "__typename": "IssueComment",
                "body": UPDATED,
                "created_at": {"$date": "2014-08-28T21:10:57.000Z"},
            },
            {
                "__typename": "IssueComment",
                "body": BUILDBOT,
                "created_at": {"$date": "2014-08-28T21:14:54.000Z"},
            },
            {
                "__typename": "IssueComment",
                "body": "   ",
            },
            {
                "__typename": "IssueComment",
                "body": MERGED_NOTE,
                "created_at": {"$date": "2014-08-28T23:24:15.000Z"},
            },
        ],
    }


def _rest_3135() -> dict:
    return {
        "node_id": NODE_ID,
        "title": "Fix override decorator",
        "state": "closed",
        "merged": False,
        "body": DESCRIPTION,
        "base": {"repo": {"full_name": "django/django"}},
        "issue_comments": [
            {"body": MERGED_NOTE, "created_at": "2014-08-28T23:24:15Z"},
            {"body": None, "created_at": "2014-08-28T23:30:00Z"},
            {"body": "   ", "created_at": "2014-08-28T23:31:00Z"},
            {"body": BUILDBOT, "created_at": "2014-08-28T21:14:54Z"},
            {"body": UPDATED, "created_at": "2014-08-28T21:10:57Z"},
            {"body": USEFUL, "created_at": "2014-08-28T20:48:15Z"},
        ],
        "reviews": [
            {"body": "ship it from the review body", "submitted_at": "2014-08-28T21:00:00Z"}
        ],
        "threads": [
            {
                "id": 9,
                "comments": [
                    {
                        "body": SAME,
                        "path": TRANSLATION,
                        "line": None,
                        "created_at": "2014-08-28T20:44:04Z",
                    }
                ],
            },
            {
                "id": 8,
                "comments": [
                    {
                        "body": WHY_NOT,
                        "path": TIMEZONE,
                        "created_at": "2014-08-28T20:43:52Z",
                    },
                    {
                        "body": ATTRIBUTES,
                        "path": TIMEZONE,
                        "line": None,
                        "start_line": None,
                        "created_at": "2014-08-28T20:47:36Z",
                    },
                ],
            },
        ],
    }
