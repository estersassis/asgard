"""Pull request listing, without the network."""

import io
import json
import urllib.error
import urllib.request
from datetime import date

import pytest

from asgard.core.config import ConfigError
from asgard.core.github import list_pulls

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
