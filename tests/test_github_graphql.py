"""GraphQL mining acceptance tests, without the network."""

from __future__ import annotations

import io
import json
import math
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from test_normalize import (  # pytest puts tests/ on sys.path
    ATTRIBUTES,
    BUILDBOT,
    DESCRIPTION,
    MERGED_NOTE,
    NODE_ID,
    SAME,
    TIMEZONE,
    TRANSLATION,
    UPDATED,
    USEFUL,
    WHY_NOT,
    _rest_3135,
)

from asgard.core.config import Filters
from asgard.core.github_graphql import mine_pulls
from asgard.core.normalization import format_pull

TOKEN = "ghp_secret"
_GRAPHQL = "https://api.github.com/graphql"
REPO = "jpadilla/pyjwt"


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


def _empty_connection() -> dict[str, object]:
    return {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}


def _connection(
    nodes: list[dict[str, object]],
    *,
    has_next: bool = False,
    cursor: str | None = None,
) -> dict[str, object]:
    return {
        "pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
        "nodes": nodes,
    }


def _node(
    number: int,
    day: str,
    *,
    updated_at: str = "2024-01-01T00:00:00Z",
    body: str = "",
    state: str = "OPEN",
    merged: bool = False,
    title: str = "",
    comments: dict[str, object] | None = None,
    review_threads: dict[str, object] | None = None,
    node_id: str | None = None,
    repo: str = REPO,
) -> dict[str, object]:
    return {
        "id": node_id or f"PR_node_{number}",
        "number": number,
        "title": title or f"pull {number}",
        "body": body,
        "state": state,
        "merged": merged,
        "createdAt": f"{day}T00:00:00Z",
        "updatedAt": updated_at,
        "baseRepository": {"nameWithOwner": repo},
        "comments": comments if comments is not None else _empty_connection(),
        "reviewThreads": (
            review_threads if review_threads is not None else _empty_connection()
        ),
    }


def _rate_limit(*, cost: int = 1, remaining: int = 5000) -> dict[str, object]:
    return {
        "cost": cost,
        "remaining": remaining,
        "resetAt": "2024-01-01T01:00:00Z",
    }


def _bulk_page(
    nodes: list[dict[str, object]],
    *,
    has_next: bool = False,
    cursor: str | None = None,
    cost: int = 1,
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
                "rateLimit": _rate_limit(cost=cost),
            }
        }
    ).encode()


def _overflow_page(
    *,
    comments: dict[str, object] | None = None,
    threads: dict[str, object] | None = None,
    cost: int = 1,
) -> bytes:
    pull: dict[str, object] = {}
    if comments is not None:
        pull["comments"] = comments
    if threads is not None:
        pull["reviewThreads"] = threads
    return json.dumps(
        {
            "data": {
                "repository": {"pullRequest": pull},
                "rateLimit": _rate_limit(cost=cost),
            }
        }
    ).encode()


def _thread_comments_page(
    nodes: list[dict[str, object]],
    *,
    has_next: bool = False,
    cursor: str | None = None,
    cost: int = 1,
) -> bytes:
    return json.dumps(
        {
            "data": {
                "node": {
                    "comments": _connection(nodes, has_next=has_next, cursor=cursor),
                },
                "rateLimit": _rate_limit(cost=cost),
            }
        }
    ).encode()


def _graphql_body(request: urllib.request.Request) -> dict[str, object]:
    raw = request.data
    assert isinstance(raw, bytes)
    payload = json.loads(raw.decode())
    assert isinstance(payload, dict)
    return payload


def _query_kind(query: str) -> str:
    if "pullRequests(" in query.replace(" ", ""):
        return "bulk"
    if "pullRequest(number" in query.replace(" ", ""):
        return "overflow"
    if "node(id" in query.replace(" ", ""):
        return "thread"
    raise AssertionError(f"unknown GraphQL query shape: {query[:80]}")


def _graphql_3135_node() -> dict[str, object]:
    """GraphQL node for the reduced django PR 3135 fixture."""
    return _node(
        3135,
        "2014-08-28",
        updated_at="2014-08-28T23:24:15Z",
        body=DESCRIPTION,
        state="CLOSED",
        title="Fix override decorator",
        node_id=NODE_ID,
        repo="django/django",
        comments=_connection(
            [
                {"body": USEFUL, "createdAt": "2014-08-28T20:48:15Z"},
                {"body": UPDATED, "createdAt": "2014-08-28T21:10:57Z"},
                {"body": BUILDBOT, "createdAt": "2014-08-28T21:14:54Z"},
                {"body": "   ", "createdAt": "2014-08-28T23:20:00Z"},
                {"body": MERGED_NOTE, "createdAt": "2014-08-28T23:24:15Z"},
            ]
        ),
        review_threads=_connection(
            [
                {
                    "id": "thread_timezone",
                    "path": TIMEZONE,
                    "comments": _connection(
                        [
                            {
                                "body": WHY_NOT,
                                "line": None,
                                "startLine": None,
                                "createdAt": "2014-08-28T20:43:52Z",
                            },
                            {
                                "body": ATTRIBUTES,
                                "line": None,
                                "startLine": None,
                                "createdAt": "2014-08-28T20:47:36Z",
                            },
                        ]
                    ),
                },
                {
                    "id": "thread_translation",
                    "path": TRANSLATION,
                    "comments": _connection(
                        [
                            {
                                "body": SAME,
                                "line": None,
                                "startLine": None,
                                "createdAt": "2014-08-28T20:44:04Z",
                            }
                        ]
                    ),
                },
            ]
        ),
    )


def test_request_count_is_ceil_of_prs_over_page_size(tmp_path: Path) -> None:
    page_size = 2
    total = 5
    numbered = [_node(number, "2024-03-01") for number in range(total, 0, -1)]
    calls: list[dict[str, object]] = []

    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        assert request.full_url == _GRAPHQL
        body = _graphql_body(request)
        assert _query_kind(str(body["query"])) == "bulk"
        variables = body["variables"]
        assert isinstance(variables, dict)
        calls.append(variables)
        assert variables["pageSize"] == page_size
        cursor = variables.get("cursor")
        if cursor is None:
            return _Response(
                _bulk_page(numbered[:page_size], has_next=True, cursor="c1")
            )
        if cursor == "c1":
            return _Response(
                _bulk_page(numbered[page_size : 2 * page_size], has_next=True, cursor="c2")
            )
        if cursor == "c2":
            return _Response(_bulk_page(numbered[2 * page_size :], has_next=False))
        raise AssertionError(f"unexpected cursor {cursor!r}")

    numbers, metrics = mine_pulls(
        tmp_path,
        REPO,
        TOKEN,
        page_size=page_size,
        opener=opener,
        sleep=lambda _: None,
    )
    assert numbers == [5, 4, 3, 2, 1]
    assert len(calls) == math.ceil(total / page_size)
    assert metrics["api_requests"] == math.ceil(total / page_size)
    assert metrics["overflow_followups"] == 0
    assert metrics["page_size"] == page_size


def test_overflow_pages_nested_connections_until_complete(tmp_path: Path) -> None:
    kinds: list[str] = []

    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        body = _graphql_body(request)
        kind = _query_kind(str(body["query"]))
        kinds.append(kind)
        variables = body["variables"]
        assert isinstance(variables, dict)
        if kind == "bulk":
            return _Response(
                _bulk_page(
                    [
                        _node(
                            9,
                            "2024-02-01",
                            body="root",
                            comments=_connection(
                                [{"body": "comment-a", "createdAt": "2024-02-01T01:00:00Z"}],
                                has_next=True,
                                cursor="comments_1",
                            ),
                            review_threads=_connection(
                                [
                                    {
                                        "id": "thread_1",
                                        "path": "a.py",
                                        "comments": _connection(
                                            [
                                                {
                                                    "body": "thread-a",
                                                    "line": 3,
                                                    "startLine": 1,
                                                    "createdAt": "2024-02-01T02:00:00Z",
                                                }
                                            ],
                                            has_next=True,
                                            cursor="tc_1",
                                        ),
                                    }
                                ],
                                has_next=True,
                                cursor="threads_1",
                            ),
                        )
                    ]
                )
            )
        if kind == "overflow":
            assert variables["number"] == 9
            assert variables["fetchComments"] is True
            assert variables["fetchThreads"] is True
            assert variables["commentsCursor"] == "comments_1"
            assert variables["threadsCursor"] == "threads_1"
            return _Response(
                _overflow_page(
                    comments=_connection(
                        [{"body": "comment-b", "createdAt": "2024-02-01T03:00:00Z"}],
                        has_next=False,
                    ),
                    threads=_connection(
                        [
                            {
                                "id": "thread_2",
                                "path": "b.py",
                                "comments": _connection(
                                    [
                                        {
                                            "body": "thread-b",
                                            "line": None,
                                            "startLine": 2,
                                            "createdAt": "2024-02-01T04:00:00Z",
                                        }
                                    ]
                                ),
                            }
                        ],
                        has_next=False,
                    ),
                )
            )
        if kind == "thread":
            assert variables["id"] == "thread_1"
            assert variables["cursor"] == "tc_1"
            return _Response(
                _thread_comments_page(
                    [
                        {
                            "body": "thread-a-reply",
                            "line": 4,
                            "startLine": 1,
                            "createdAt": "2024-02-01T02:30:00Z",
                        }
                    ]
                )
            )
        raise AssertionError(kind)

    numbers, metrics = mine_pulls(
        tmp_path,
        REPO,
        TOKEN,
        opener=opener,
        sleep=lambda _: None,
    )
    assert numbers == [9]
    assert kinds == ["bulk", "overflow", "thread"]
    assert metrics["api_requests"] == 3
    assert metrics["overflow_followups"] == 2

    saved = json.loads((tmp_path / "mine" / "prs" / "9.json").read_text(encoding="utf-8"))
    timeline = saved["timeline_items"]
    comments = [item for item in timeline if item["__typename"] == "IssueComment"]
    threads = [item for item in timeline if item["__typename"] == "PullRequestReviewThread"]
    assert [item["body"] for item in comments] == ["comment-a", "comment-b"]
    assert {thread["path"] for thread in threads} == {"a.py", "b.py"}
    thread_a = next(thread for thread in threads if thread["path"] == "a.py")
    assert [comment["body"] for comment in thread_a["comments"]] == [
        "thread-a",
        "thread-a-reply",
    ]
    assert thread_a["comments"][0]["start_line"] == 1
    assert thread_a["comments"][0]["line"] == 3


def test_graphql_dump_matches_rest_format_pull(tmp_path: Path) -> None:
    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        body = _graphql_body(request)
        assert _query_kind(str(body["query"])) == "bulk"
        return _Response(_bulk_page([_graphql_3135_node()]))

    numbers, _metrics = mine_pulls(
        tmp_path,
        "django/django",
        TOKEN,
        opener=opener,
        sleep=lambda _: None,
    )
    assert numbers == [3135]
    mined = json.loads((tmp_path / "mine" / "prs" / "3135.json").read_text(encoding="utf-8"))
    from_graphql = format_pull(mined)
    from_rest = format_pull(_rest_3135(), repo="other/repo")
    assert from_graphql == from_rest
    assert "ship it from the review body" not in str(from_graphql)


def test_filters_since_until_state_and_max_prs(tmp_path: Path) -> None:
    seen_states: list[Any] = []

    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        body = _graphql_body(request)
        variables = body["variables"]
        assert isinstance(variables, dict)
        seen_states.append(variables.get("states"))
        return _Response(
            _bulk_page(
                [
                    _node(3, "2024-03-01", body="new", state="OPEN"),
                    _node(2, "2024-02-01", body="kept", state="OPEN"),
                    _node(1, "2024-01-01", body="old", state="OPEN"),
                ]
            )
        )

    numbers, _metrics = mine_pulls(
        tmp_path,
        REPO,
        TOKEN,
        filters=Filters(
            state="open",
            since=date(2024, 1, 15),
            until=date(2024, 2, 15),
        ),
        opener=opener,
        sleep=lambda _: None,
    )
    assert numbers == [2]
    assert seen_states == [["OPEN"]]
    assert json.loads((tmp_path / "mine" / "prs" / "2.json").read_text(encoding="utf-8"))[
        "body"
    ] == "kept"

    seen_states.clear()

    def closed_opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        body = _graphql_body(request)
        variables = body["variables"]
        assert isinstance(variables, dict)
        seen_states.append(variables.get("states"))
        return _Response(
            _bulk_page(
                [
                    _node(9, "2024-04-01", state="CLOSED"),
                    _node(8, "2024-04-01", state="MERGED", merged=True),
                    _node(7, "2024-04-01", state="CLOSED"),
                ]
            )
        )

    limited, _metrics = mine_pulls(
        tmp_path / "closed",
        REPO,
        TOKEN,
        filters=Filters(state="closed", max_prs=2),
        opener=closed_opener,
        sleep=lambda _: None,
    )
    assert limited == [9, 8]
    assert seen_states == [["CLOSED", "MERGED"]]

    seen_states.clear()

    def all_opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        body = _graphql_body(request)
        variables = body["variables"]
        assert isinstance(variables, dict)
        seen_states.append(variables.get("states", "missing"))
        assert "states" not in variables
        return _Response(_bulk_page([]))

    empty, _metrics = mine_pulls(
        tmp_path / "all",
        REPO,
        TOKEN,
        filters=Filters(state="all"),
        opener=all_opener,
        sleep=lambda _: None,
    )
    assert empty == []
    assert seen_states == ["missing"]


def test_http_502_halves_page_size_and_retries_same_cursor(tmp_path: Path) -> None:
    attempts: list[tuple[Any, int]] = []
    fail_once = {"value": True}

    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        body = _graphql_body(request)
        assert _query_kind(str(body["query"])) == "bulk"
        variables = body["variables"]
        assert isinstance(variables, dict)
        cursor = variables.get("cursor")
        page_size = int(variables["pageSize"])
        attempts.append((cursor, page_size))
        if cursor is None and page_size == 50 and fail_once["value"]:
            fail_once["value"] = False
            raise urllib.error.HTTPError(
                request.full_url,
                502,
                "Bad Gateway",
                hdrs=None,
                fp=io.BytesIO(b"bad gateway"),
            )
        if cursor is None and page_size == 25:
            return _Response(
                _bulk_page(
                    [_node(2, "2024-02-01"), _node(1, "2024-02-01")],
                    has_next=False,
                )
            )
        raise AssertionError(f"unexpected attempt cursor={cursor!r} page_size={page_size}")

    numbers, metrics = mine_pulls(
        tmp_path,
        REPO,
        TOKEN,
        page_size=50,
        opener=opener,
        sleep=lambda _: None,
    )
    assert numbers == [2, 1]
    assert attempts == [(None, 50), (None, 25)]
    assert metrics["page_size_reductions"] == 1
    assert metrics["api_requests"] == 1
    assert not (tmp_path / "mine" / "cursor.json").exists()


def test_resume_uses_cursor_and_skips_cache_hits(tmp_path: Path) -> None:
    mine_dir = tmp_path / "mine"
    pr_dir = mine_dir / "prs"
    pr_dir.mkdir(parents=True)
    cached = {
        "base_repository": REPO,
        "id": "PR_node_5",
        "number": 5,
        "title": "cached",
        "state": "OPEN",
        "merged": False,
        "body": "already here",
        "updated_at": "2024-01-01T00:00:00Z",
        "timeline_items": [],
    }
    (pr_dir / "5.json").write_text(json.dumps(cached), encoding="utf-8")
    (mine_dir / "cursor.json").write_text(
        json.dumps({"cursor": "resume_here", "page_size": 50}),
        encoding="utf-8",
    )
    before = (pr_dir / "5.json").read_text(encoding="utf-8")
    kinds: list[str] = []

    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        body = _graphql_body(request)
        kind = _query_kind(str(body["query"]))
        kinds.append(kind)
        variables = body["variables"]
        assert isinstance(variables, dict)
        assert variables.get("cursor") == "resume_here"
        assert variables["pageSize"] == 50
        # Overflow markers on the cached PR must not trigger follow-ups on a hit.
        return _Response(
            _bulk_page(
                [
                    _node(
                        5,
                        "2024-01-01",
                        updated_at="2024-01-01T00:00:00Z",
                        body="should not overwrite",
                        comments=_connection(
                            [{"body": "ignored", "createdAt": "2024-01-01T01:00:00Z"}],
                            has_next=True,
                            cursor="never",
                        ),
                    ),
                    _node(4, "2024-01-01", body="fresh"),
                ]
            )
        )

    numbers, metrics = mine_pulls(
        tmp_path,
        REPO,
        TOKEN,
        opener=opener,
        sleep=lambda _: None,
    )
    assert numbers == [5, 4]
    assert kinds == ["bulk"]
    assert metrics["overflow_followups"] == 0
    assert (pr_dir / "5.json").read_text(encoding="utf-8") == before
    fresh = json.loads((pr_dir / "4.json").read_text(encoding="utf-8"))
    assert fresh["body"] == "fresh"
    assert not (mine_dir / "cursor.json").exists()


def test_graphql_errors_with_partial_data_are_not_accepted(tmp_path: Path) -> None:
    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        return _Response(
            json.dumps(
                {
                    "data": {
                        "repository": {
                            "pullRequests": {
                                "pageInfo": {"hasNextPage": False, "endCursor": None},
                                "nodes": [_node(1, "2024-01-01")],
                            }
                        },
                        "rateLimit": _rate_limit(),
                    },
                    "errors": [{"message": "Something went wrong"}],
                }
            ).encode()
        )

    with pytest.raises(Exception, match="page_size=1"):
        mine_pulls(
            tmp_path,
            REPO,
            TOKEN,
            page_size=1,
            opener=opener,
            sleep=lambda _: None,
        )
    assert list((tmp_path / "mine" / "prs").glob("*.json")) == []
