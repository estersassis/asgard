"""Full pipeline from recorded pyjwt REST fixtures. No network."""

import json
import urllib.request
from pathlib import Path

import pytest

from asgard.cli.main import main
from asgard.providers.handlers.fake import FakeHandler

TOKEN = "ghp_secret"
FIXTURES = Path(__file__).parent / "fixtures" / "pyjwt"
_API = "https://api.github.com/repos/jpadilla/pyjwt"
_LIST = f"{_API}/pulls?state=all&per_page=100&sort=created&direction=desc&page=1"
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


def _routes(numbers: list[int]) -> dict[str, Path]:
    routes = {
        _API: FIXTURES / "repo.json",
        _LIST: FIXTURES / "pulls.json",
    }
    for number in numbers:
        routes[f"{_API}/pulls/{number}"] = FIXTURES / f"{number}.json"
        routes[f"{_API}/issues/{number}/comments?per_page=100&page=1"] = (
            FIXTURES / "comments.json"
        )
        routes[f"{_API}/pulls/{number}/reviews?per_page=100&page=1"] = FIXTURES / "reviews.json"
        routes[f"{_API}/pulls/{number}/comments?per_page=100&page=1"] = (
            FIXTURES / "review_comments.json"
        )
    return routes


def _opener(numbers: list[int], seen: list[str]):
    routes = _routes(numbers)

    def opener(request: urllib.request.Request, timeout: int = 10) -> _Response:
        url = request.full_url
        path = routes.get(url)
        if path is None:
            raise AssertionError(f"unexpected URL: {url}")
        assert request.get_header("Authorization") == f"Bearer {TOKEN}"
        seen.append(url)
        return _Response(path.read_bytes())

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
    synthesis = json.dumps(
        {
            "topics": [
                {
                    "title": "Authentication",
                    "recommendations": [
                        {
                            "title": "Keep the algorithm allow-list a sequence",
                            "text": "Reject a bare string so the allow-list cannot be bypassed.",
                            "pr_ids": [str(security)],
                        }
                    ],
                }
            ]
        }
    )
    return FakeHandler([approved, verdict, rejected, verdict, synthesis])


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

    assert seen == list(_routes(numbers))
    link = f"https://github.com/jpadilla/pyjwt/pull/{security}"
    written = (out / "SECURITY.md").read_text(encoding="utf-8")
    assert link in written
    assert f"https://github.com/jpadilla/pyjwt/pull/{documentation}" not in written
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["counts"]["mined"] == 2
    assert manifest["counts"]["security"] == 1
    for path in out.rglob("*"):
        if path.is_file():
            assert TOKEN not in path.read_text(encoding="utf-8")

    with pytest.raises(AssertionError, match="unexpected URL"):
        opener(urllib.request.Request(f"{_API}/pulls/1"), timeout=10)
    with pytest.raises(AssertionError, match="network is disabled"):
        urllib.request.urlopen(f"{_API}/pulls/1")
