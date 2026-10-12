"""GitHub REST calls used by the pipeline."""

import json
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

from asgard.core.config import ConfigError

_PAGE_SIZE = 100


def list_pulls(
    repo: str,
    token: str,
    *,
    state: str = "all",
    since: date | None = None,
    until: date | None = None,
    max_prs: int | None = None,
    opener: Any = None,
) -> list[dict[str, Any]]:
    """List pull requests, newest first, until the filters are satisfied."""
    open_url = opener or urllib.request.urlopen
    found: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = _fetch_page(repo, token, state, page, open_url)
        if not batch:
            return found
        for pr in batch:
            created = _created_on(pr)
            if since or until:
                if created is None or (until and created > until):
                    continue
                if since and created < since:
                    return found
            found.append(pr)
            if max_prs is not None and len(found) >= max_prs:
                return found
        if len(batch) < _PAGE_SIZE:
            return found
        page += 1


def cache_pulls(
    run_dir: Path,
    repo: str,
    token: str,
    pulls: list[dict[str, Any]],
    *,
    opener: Any = None,
) -> list[int]:
    """Store each pull under ``run_dir/mine/prs`` and return their numbers.

    A file is reused when its ``updated_at`` matches the listing. An empty
    listing writes an empty number list and does not fetch.
    """
    pr_dir = Path(run_dir) / "mine" / "prs"
    pr_dir.mkdir(parents=True, exist_ok=True)
    numbers: list[int] = []
    for pr in pulls:
        number = pr.get("number")
        if not isinstance(number, int):
            raise ConfigError("github pull number was not an int")
        numbers.append(number)
        path = pr_dir / f"{number}.json"
        listed_at = pr.get("updated_at")
        if _cache_hit(path, listed_at):
            continue
        fetched = fetch_pull(repo, number, token, opener=opener)
        fetched["updated_at"] = listed_at
        _write_json(path, fetched)
    _write_json(Path(run_dir) / "mine" / "output.json", numbers)
    return numbers


def fetch_pull(
    repo: str,
    number: int,
    token: str,
    *,
    opener: Any = None,
) -> dict[str, Any]:
    """Fetch one pull request with its comments, reviews, and review threads."""
    open_url = opener or urllib.request.urlopen
    pull = _get_json(repo, token, f"pulls/{number}", open_url)
    if not isinstance(pull, dict):
        raise ConfigError("github pull was not an object")
    issue_comments = _fetch_list(repo, token, f"issues/{number}/comments", open_url)
    reviews = _fetch_list(repo, token, f"pulls/{number}/reviews", open_url)
    review_comments = _fetch_list(repo, token, f"pulls/{number}/comments", open_url)
    return {
        **pull,
        "issue_comments": issue_comments,
        "reviews": reviews,
        "threads": _threads(review_comments),
    }


def _fetch_page(repo: str, token: str, state: str, page: int, opener: Any) -> list[dict[str, Any]]:
    owner, name = repo.split("/", 1)
    url = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(name, safe='')}/pulls"
        f"?state={quote(state, safe='')}&per_page={_PAGE_SIZE}"
        f"&sort=created&direction=desc&page={page}"
    )
    payload = _read_json(url, token, opener)
    if not isinstance(payload, list):
        raise ConfigError("github pull list was not a list")
    return payload


def _fetch_list(repo: str, token: str, path: str, opener: Any) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = _get_json(repo, token, f"{path}?per_page={_PAGE_SIZE}&page={page}", opener)
        if not isinstance(payload, list):
            raise ConfigError("github response was not a list")
        found.extend(payload)
        if len(payload) < _PAGE_SIZE:
            return found
        page += 1


def _get_json(repo: str, token: str, path: str, opener: Any) -> Any:
    owner, name = repo.split("/", 1)
    url = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(name, safe='')}/{path}"
    )
    return _read_json(url, token, opener)


def _read_json(url: str, token: str, opener: Any) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": "asgard",
        },
    )
    try:
        with opener(request, timeout=10) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise ConfigError(_github_error(exc, token)) from None


def _cache_hit(path: Path, updated_at: Any) -> bool:
    if not isinstance(updated_at, str) or not path.is_file():
        return False
    try:
        cached = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return isinstance(cached, dict) and cached.get("updated_at") == updated_at


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload), encoding="utf-8")
    temporary.replace(path)


def _threads(comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group review comments by the thread root. Replies may arrive on a later page."""
    by_id = {
        comment["id"]: comment
        for comment in comments
        if isinstance(comment.get("id"), int)
    }
    groups: dict[int, list[dict[str, Any]]] = {}
    order: list[int] = []
    for comment in comments:
        root = _thread_root(comment, by_id)
        if root not in groups:
            groups[root] = []
            order.append(root)
        if comment.get("id") == root:
            groups[root].insert(0, comment)
        else:
            groups[root].append(comment)
    return [{"id": root, "comments": groups[root]} for root in order]


def _thread_root(comment: dict[str, Any], by_id: dict[int, dict[str, Any]]) -> int:
    seen: set[int] = set()
    current = comment
    while isinstance(current.get("in_reply_to_id"), int):
        parent_id = current["in_reply_to_id"]
        if parent_id in seen:
            break
        seen.add(parent_id)
        parent = by_id.get(parent_id)
        if parent is None:
            return parent_id
        current = parent
    comment_id = current.get("id")
    if isinstance(comment_id, int):
        return comment_id
    return 0


def _created_on(pr: dict[str, Any]) -> date | None:
    raw = pr.get("created_at")
    if not isinstance(raw, str) or len(raw) < 10:
        return None
    return date.fromisoformat(raw[:10])


def _github_error(exc: urllib.error.HTTPError, token: str) -> str:
    if exc.code == 401:
        return "invalid GitHub token"
    if exc.code == 404:
        return "repository not found or token cannot see it"
    detail = ""
    if exc.fp is not None:
        detail = exc.read().decode(errors="replace")[:200]
    message = f"github is not reachable: HTTP {exc.code} {detail}".strip()
    return message.replace(token, "***") if token else message
