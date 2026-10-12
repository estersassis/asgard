"""One page of pull request metadata, without the network."""

import io
import json
import urllib.error
import urllib.request

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
    assert seen == [
        "https://api.github.com/repos/jpadilla/pyjwt/pulls?state=closed&per_page=100"
    ]


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
