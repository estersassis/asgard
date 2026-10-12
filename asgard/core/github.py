"""GitHub REST calls used by the pipeline."""

import json
import urllib.error
import urllib.request
from datetime import date
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


def _fetch_page(repo: str, token: str, state: str, page: int, opener: Any) -> list[dict[str, Any]]:
    owner, name = repo.split("/", 1)
    url = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(name, safe='')}/pulls"
        f"?state={quote(state, safe='')}&per_page={_PAGE_SIZE}"
        f"&sort=created&direction=desc&page={page}"
    )
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
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise ConfigError(_github_error(exc, token)) from None
    if not isinstance(payload, list):
        raise ConfigError("github pull list was not a list")
    return payload


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
