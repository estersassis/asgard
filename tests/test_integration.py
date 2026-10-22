"""Full pipeline from recorded pyjwt fixtures via GraphQL. No network."""

import json
import urllib.request
from pathlib import Path

import pytest

from asgard.cli.main import main
from asgard.providers.handlers.fake import FakeHandler

TOKEN = "ghp_secret"
FIXTURES = Path(__file__).parent / "fixtures" / "pyjwt"
_API = "https://api.github.com/repos/jpadilla/pyjwt"
_GRAPHQL = "https://api.github.com/graphql"
_QUOTE = "the allow-list is what prevents algorithm-confusion"


class _Response:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self._body = body
        self.headers = headers or {"X-OAuth-Scopes": "public_repo"}

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _blocked(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("network is disabled")


def _numbers() -> list[int]:
    listed = json.loads((FIXTURES / "pulls.json").read_text(encoding="utf-8"))
    return [item["number"] for item in listed]


def _empty_connection() -> dict[str, object]:
    return {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}


def _graphql_state(rest: dict[str, object]) -> str:
    if rest.get("merged") is True:
        return "MERGED"
    state = rest.get("state")
    if state == "open":
        return "OPEN"
    if state == "closed":
        return "CLOSED"
    return "OPEN"


def _node_from_fixture(number: int) -> dict[str, object]:
    rest = json.loads((FIXTURES / f"{number}.json").read_text(encoding="utf-8"))
    listed = next(
        item
        for item in json.loads((FIXTURES / "pulls.json").read_text(encoding="utf-8"))
        if item["number"] == number
    )
    return {
        "id": rest.get("node_id") or f"PR_node_{number}",
        "number": number,
        "title": rest.get("title") or listed.get("title") or "",
        "body": rest.get("body") or "",
        "state": _graphql_state(rest),
        "merged": rest.get("merged") is True,
        "createdAt": rest.get("created_at") or listed.get("created_at"),
        "updatedAt": rest.get("updated_at") or listed.get("updated_at"),
        "baseRepository": {"nameWithOwner": "jpadilla/pyjwt"},
        "comments": _empty_connection(),
        "reviewThreads": _empty_connection(),
    }


def _graphql_page(numbers: list[int]) -> bytes:
    return json.dumps(
        {
            "data": {
                "repository": {
                    "pullRequests": {
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                        "nodes": [_node_from_fixture(number) for number in numbers],
                    }
                },
                "rateLimit": {
                    "cost": 1,
                    "remaining": 5000,
                    "resetAt": "2024-01-01T01:00:00Z",
                },
            }
        }
    ).encode()


def _opener(numbers: list[int], seen: list[str]):
    repo_body = (FIXTURES / "repo.json").read_bytes()

    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        url = request.full_url
        if url not in (_API, _GRAPHQL):
            raise AssertionError(f"unexpected URL: {url}")
        assert request.get_header("Authorization") == f"Bearer {TOKEN}"
        seen.append(url)
        if url == _API:
            return _Response(repo_body)
        return _Response(_graphql_page(numbers))

    return opener


def _findings() -> list[dict[str, object]]:
    return [
        {"criterion": name, "passed": True, "explanation": f"{name} checked"}
        for name in ("decision", "evidence", "completeness", "rationale")
    ]


def _handler(security: int) -> FakeHandler:
    approved = json.dumps(
        {
            "has_security_discussion": True,
            "evidence": [{"quote": _QUOTE, "location": "description"}],
            "rationale": "the description discusses authentication",
        }
    )
    rejected = json.dumps(
        {
            "has_security_discussion": False,
            "evidence": [],
            "rationale": "documentation only",
        }
    )
    verdict = json.dumps({"verdict": "APPROVE", "findings": _findings(), "feedback": ""})
    mapped = json.dumps(
        {
            "items": [
                {
                    "owasp": "A07",
                    "title": "Keep the algorithm allow-list a sequence.",
                    "risk": "A bare string can bypass the allow-list.",
                    "actions": ["Keep the allow-list a sequence."],
                    "pr_ids": [str(security)],
                }
            ]
        }
    )
    synthesis = json.dumps(
        {
            "recommendations": [
                {
                    "owasp": "A07",
                    "title": "Keep the algorithm allow-list a sequence",
                    "risk": "A bare string can bypass the allow-list.",
                    "actions": ["Reject a bare string so the allow-list cannot be bypassed."],
                    "sources": ["1"],
                }
            ]
        }
    )
    return FakeHandler([approved, verdict, rejected, verdict, mapped, synthesis])


def test_recorded_fixtures_produce_security_md(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    numbers = _numbers()
    assert len(numbers) == 2
    security, documentation = numbers
    auth = json.loads((FIXTURES / f"{security}.json").read_text(encoding="utf-8"))
    docs = json.loads((FIXTURES / f"{documentation}.json").read_text(encoding="utf-8"))
    assert _QUOTE in auth["body"]
    assert "documentation" in docs["body"]
    for name in ("comments.json", "reviews.json", "review_comments.json"):
        assert json.loads((FIXTURES / name).read_text(encoding="utf-8")) == []

    monkeypatch.setenv("GITHUB_TOKEN", TOKEN)
    monkeypatch.setattr("asgard.core.config.load_dotenv", lambda path=Path(".env"): None)
    monkeypatch.setattr(urllib.request, "urlopen", _blocked)
    out = tmp_path / "out"
    source = tmp_path / "asgard.toml"
    source.write_text(
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
    seen: list[str] = []
    opener = _opener(numbers, seen)
    main(
        ["run", "--config", str(source), "--quiet"],
        opener=opener,
        handlers={"fake": _handler(security)},
    )

    assert seen == [_API, _GRAPHQL]
    link = f"https://github.com/jpadilla/pyjwt/pull/{security}"
    written = (out / "SECURITY.md").read_text(encoding="utf-8")
    assert link in written
    assert f"https://github.com/jpadilla/pyjwt/pull/{documentation}" not in written
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["counts"]["mined"] == 2
    assert manifest["counts"]["security"] == 1
    assert manifest["files"]["mine/metrics.json"] == "GraphQL mining request and cost metrics."
    for path in out.rglob("*"):
        if path.is_file():
            assert TOKEN not in path.read_text(encoding="utf-8")

    with pytest.raises(AssertionError, match="unexpected URL"):
        opener(urllib.request.Request(f"{_API}/pulls/1"), timeout=10)
    with pytest.raises(AssertionError, match="network is disabled"):
        urllib.request.urlopen(f"{_API}/pulls/1")
