"""GitHub REST calls used by the pipeline."""

import json
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote

from asgard.core.config import ConfigError


def list_pulls(
    repo: str,
    token: str,
    *,
    state: str = "all",
    opener: Any = None,
) -> list[dict[str, Any]]:
    open_url = opener or urllib.request.urlopen
    owner, name = repo.split("/", 1)
    url = (
        f"https://api.github.com/repos/{quote(owner, safe='')}/{quote(name, safe='')}"
        f"/pulls?state={quote(state, safe='')}&per_page=100"
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
        with open_url(request, timeout=10) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        raise ConfigError(_github_error(exc, token)) from None
    if not isinstance(payload, list):
        raise ConfigError("github pull list was not a list")
    return payload


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
