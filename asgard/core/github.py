"""GitHub REST calls used by the pipeline."""

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import quote

from asgard.core.config import ConfigError
from asgard.core.events import Error, Event, RateLimited

_ANONYMOUS_LIMIT = "warning: anonymous GitHub rate limit is 60 requests per hour"

_PAGE_SIZE = 100


def list_pulls(
    repo: str,
    token: str | None,
    *,
    state: str = "all",
    since: date | None = None,
    until: date | None = None,
    max_prs: int | None = None,
    opener: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    emit: Callable[[Event], None] | None = None,
    now: Callable[[], float] = time.time,
) -> list[dict[str, Any]]:
    """List pull requests, newest first, until the filters are satisfied."""
    open_url = opener or urllib.request.urlopen
    transport = _Transport(sleep, emit, now)
    found: list[dict[str, Any]] = []
    page = 1
    while True:
        batch = _fetch_page(repo, token, state, page, open_url, transport)
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
    token: str | None,
    pulls: list[dict[str, Any]],
    *,
    opener: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    emit: Callable[[Event], None] | None = None,
    now: Callable[[], float] = time.time,
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
        fetched = fetch_pull(
            repo,
            number,
            token,
            opener=opener,
            sleep=sleep,
            emit=emit,
            now=now,
        )
        fetched["updated_at"] = listed_at
        _write_json(path, fetched)
    _write_json(Path(run_dir) / "mine" / "output.json", numbers)
    return numbers


def fetch_pull(
    repo: str,
    number: int,
    token: str | None,
    *,
    opener: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    emit: Callable[[Event], None] | None = None,
    now: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Fetch one pull request with its comments, reviews, and review threads."""
    open_url = opener or urllib.request.urlopen
    transport = _Transport(sleep, emit, now)
    pull = _get_json(repo, token, f"pulls/{number}", open_url, transport)
    if not isinstance(pull, dict):
        raise ConfigError("github pull was not an object")
    issue_comments = _fetch_list(repo, token, f"issues/{number}/comments", open_url, transport)
    reviews = _fetch_list(repo, token, f"pulls/{number}/reviews", open_url, transport)
    review_comments = _fetch_list(repo, token, f"pulls/{number}/comments", open_url, transport)
    return {
        **pull,
        "issue_comments": issue_comments,
        "reviews": reviews,
        "threads": _threads(review_comments),
    }


def _fetch_page(
    repo: str,
    token: str | None,
    state: str,
    page: int,
    opener: Any,
    transport: "_Transport",
) -> list[dict[str, Any]]:
    owner, name = repo.split("/", 1)
    url = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(name, safe='')}/pulls"
        f"?state={quote(state, safe='')}&per_page={_PAGE_SIZE}"
        f"&sort=created&direction=desc&page={page}"
    )
    payload = _read_json(url, token, opener, transport)
    if not isinstance(payload, list):
        raise ConfigError("github pull list was not a list")
    return payload


def _fetch_list(
    repo: str,
    token: str | None,
    path: str,
    opener: Any,
    transport: "_Transport",
) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    page = 1
    while True:
        payload = _get_json(
            repo,
            token,
            f"{path}?per_page={_PAGE_SIZE}&page={page}",
            opener,
            transport,
        )
        if not isinstance(payload, list):
            raise ConfigError("github response was not a list")
        found.extend(payload)
        if len(payload) < _PAGE_SIZE:
            return found
        page += 1


def _get_json(
    repo: str,
    token: str | None,
    path: str,
    opener: Any,
    transport: "_Transport",
) -> Any:
    owner, name = repo.split("/", 1)
    url = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(name, safe='')}/{path}"
    )
    return _read_json(url, token, opener, transport)


def _read_json(url: str, token: str | None, opener: Any, transport: "_Transport") -> Any:
    while True:
        request = _request(url, token)
        try:
            with opener(request, timeout=10) as response:
                payload = json.loads(response.read().decode())
                headers = getattr(response, "headers", None)
        except urllib.error.HTTPError as exc:
            headers = getattr(exc, "headers", None)
            _note_anonymous(token, headers, transport)
            if exc.code in (403, 429) and _pause(headers, transport, on_error=True):
                exc.close()
                continue
            raise ConfigError(_github_error(exc, token)) from None
        _note_anonymous(token, headers, transport)
        _pause(headers, transport, on_error=False)
        return payload


def _request(url: str, token: str | None) -> urllib.request.Request:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "asgard",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return urllib.request.Request(url, headers=headers)


class _Transport:
    def __init__(
        self,
        sleep: Callable[[float], None],
        emit: Callable[[Event], None] | None,
        now: Callable[[], float],
    ) -> None:
        self.sleep = sleep
        self.emit = emit or _ignore
        self.now = now
        self.anonymous_warned = False


def _ignore(_event: Event) -> None:
    return None


def _note_anonymous(token: str | None, headers: Any, transport: _Transport) -> None:
    if token or transport.anonymous_warned:
        return
    if _header(headers, "X-RateLimit-Limit") != "60":
        return
    transport.anonymous_warned = True
    transport.emit(Error(message=_ANONYMOUS_LIMIT))


def _pause(headers: Any, transport: _Transport, *, on_error: bool) -> bool:
    wait = _wait_seconds(headers, transport.now(), on_error=on_error)
    if wait is None or wait <= 0:
        return False
    transport.emit(RateLimited(wait_seconds=wait))
    transport.sleep(wait)
    return True


def _wait_seconds(headers: Any, now: float, *, on_error: bool) -> float | None:
    if on_error:
        retry_after = _header(headers, "Retry-After")
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
    if _header(headers, "X-RateLimit-Remaining") != "0":
        return None
    reset = _header(headers, "X-RateLimit-Reset")
    if reset is None:
        return None
    try:
        return max(0.0, float(reset) - now)
    except ValueError:
        return None


def _header(headers: Any, name: str) -> str | None:
    if headers is None:
        return None
    getter = getattr(headers, "get", None)
    if getter is None:
        return None
    value = getter(name)
    if value is None:
        value = getter(name.lower())
    if value is None:
        return None
    text = str(value).strip()
    return text or None


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


def _github_error(exc: urllib.error.HTTPError, token: str | None) -> str:
    if exc.code == 401:
        return "invalid GitHub token"
    if exc.code == 404:
        return "repository not found or token cannot see it"
    detail = ""
    if exc.fp is not None:
        detail = exc.read().decode(errors="replace")[:200]
    message = f"github is not reachable: HTTP {exc.code} {detail}".strip()
    return message.replace(token, "***") if token else message
