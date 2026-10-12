"""Pull request listing, without the network."""

import io
import json
import urllib.error
import urllib.request
from datetime import date

import pytest

from asgard.core.config import ConfigError
from asgard.core.github import fetch_pull, list_pulls

TOKEN = "ghp_secret"


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_list_pulls_keeps_api_order() -> None:
    body = json.dumps(
        [
            {"number": 2, "title": "second"},
            {"number": 1, "title": "first"},
        ]
    ).encode()
    seen: list[str] = []

    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        seen.append(request.full_url)
        assert request.get_header("Authorization") == f"Bearer {TOKEN}"
        assert request.get_header("Accept") == "application/vnd.github+json"
        return _Response(body)

    pulls = list_pulls("jpadilla/pyjwt", TOKEN, state="closed", opener=opener)
    assert [item["number"] for item in pulls] == [2, 1]
    assert "state=closed" in seen[0]
    assert "per_page=100" in seen[0]
    assert seen[0].endswith("&page=1")
    assert len(seen) == 1


def test_unauthorized_does_not_echo_the_token() -> None:
    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "no",
            hdrs=None,
            fp=io.BytesIO(TOKEN.encode()),
        )

    with pytest.raises(ConfigError, match="invalid GitHub token") as caught:
        list_pulls("jpadilla/pyjwt", TOKEN, opener=opener)
    assert TOKEN not in str(caught.value)


def test_stops_at_max_prs_and_date_range() -> None:
    def pr(number: int, day: str) -> dict[str, str | int]:
        return {"number": number, "created_at": f"{day}T00:00:00Z"}

    pages = {
        1: [pr(number, "2024-03-01") for number in range(100)],
        2: [pr(50, "2024-02-01"), pr(40, "2024-01-01")],
    }
    seen: list[int] = []

    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        page = int(request.full_url.rsplit("page=", 1)[1])
        seen.append(page)
        return _Response(json.dumps(pages.get(page, [])).encode())

    limited = list_pulls("jpadilla/pyjwt", TOKEN, max_prs=2, opener=opener)
    assert [item["number"] for item in limited] == [0, 1]
    assert seen == [1]

    seen.clear()
    dated = list_pulls(
        "jpadilla/pyjwt",
        TOKEN,
        since=date(2024, 1, 15),
        until=date(2024, 2, 15),
        opener=opener,
    )
    assert [item["number"] for item in dated] == [50]
    assert seen == [1, 2]


def test_fetch_pull_joins_discussion_and_keeps_deleted_users() -> None:
    page = [
        {"id": 1, "body": "elsewhere", "user": {"login": "ada"}, "in_reply_to_id": None},
        {"id": 2, "body": "root", "user": None, "in_reply_to_id": None},
        *[
            {"id": number, "body": f"reply {number}", "user": {"login": "ada"}, "in_reply_to_id": 2}
            for number in range(3, 101)
        ],
    ]
    seen: list[str] = []

    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        url = request.full_url
        seen.append(url)
        assert request.get_header("Authorization") == f"Bearer {TOKEN}"
        assert request.get_header("Accept") == "application/vnd.github+json"
        if url.endswith("/pulls/7"):
            body = {"number": 7, "title": "fix", "body": "the change", "user": None}
            return _Response(json.dumps(body).encode())
        if "/issues/7/comments" in url:
            return _Response(json.dumps([{"id": 9, "body": "lgtm", "user": None}]).encode())
        if "/pulls/7/reviews" in url:
            review = {"id": 8, "body": "ship it", "user": {"login": "bea"}}
            return _Response(json.dumps([review]).encode())
        page_number = int(url.rsplit("page=", 1)[1])
        if page_number == 1:
            return _Response(json.dumps(page).encode())
        reply = {"id": 101, "body": "still going", "user": None, "in_reply_to_id": 2}
        return _Response(json.dumps([reply]).encode())

    pull = fetch_pull("jpadilla/pyjwt", 7, TOKEN, opener=opener)
    assert pull["body"] == "the change"
    assert pull["user"] is None
    assert pull["issue_comments"] == [{"id": 9, "body": "lgtm", "user": None}]
    assert pull["reviews"] == [{"id": 8, "body": "ship it", "user": {"login": "bea"}}]
    assert [thread["id"] for thread in pull["threads"]] == [1, 2]
    long_thread = pull["threads"][1]["comments"]
    assert [item["id"] for item in long_thread] == list(range(2, 102))
    assert long_thread[0]["user"] is None
    assert long_thread[-1]["user"] is None
    assert seen[0].endswith("/pulls/7")
    assert "/issues/7/comments?per_page=100&page=1" in seen[1]
    assert "/pulls/7/reviews?per_page=100&page=1" in seen[2]
    assert "/pulls/7/comments?per_page=100&page=1" in seen[3]
    assert "/pulls/7/comments?per_page=100&page=2" in seen[4]
    assert len(seen) == 5


def test_fetch_pull_does_not_echo_the_token() -> None:
    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        raise urllib.error.HTTPError(
            request.full_url,
            404,
            "missing",
            hdrs=None,
            fp=io.BytesIO(TOKEN.encode()),
        )

    with pytest.raises(ConfigError, match="repository not found") as caught:
        fetch_pull("jpadilla/pyjwt", 7, TOKEN, opener=opener)
    assert TOKEN not in str(caught.value)
