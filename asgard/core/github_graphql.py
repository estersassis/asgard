"""GitHub GraphQL mining: page pull requests with nested discussion in one query."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from asgard.core.config import ConfigError, Filters
from asgard.core.events import Event, RateLimited
from asgard.core.github import (
    _cache_hit,
    _github_error,
    _pause,
    _Transport,
    _write_json,
)

_GRAPHQL_URL = "https://api.github.com/graphql"

_COMMENTS_PAGE = 50
_THREADS_PAGE = 50
_THREAD_COMMENTS_PAGE = 30

_SUCCESS_STREAK_TO_GROW = 3
_PAGE_LADDER = (50, 25, 10, 1)

_CURSOR_NAME = "cursor.json"


def mine_pulls(
    run_dir: Path,
    repo: str,
    token: str,
    *,
    filters: Filters | None = None,
    page_size: int = 50,
    opener: Any = None,
    sleep: Callable[[float], None] = time.sleep,
    emit: Callable[[Event], None] | None = None,
    now: Callable[[], float] = time.time,
) -> tuple[list[int], dict[str, Any]]:
    """Mine pull requests via GraphQL into ``run_dir/mine/prs``.

    Returns the kept pull numbers and a metrics dict. Resumes from
    ``mine/cursor.json`` when present. Unchanged pulls are skipped when the
    cached ``updated_at`` still matches.
    """
    if not token:
        raise ConfigError("github token is required for GraphQL mining")
    if page_size < 1:
        raise ConfigError("page_size must be >= 1")

    filters = filters or Filters()
    open_url = opener or urllib.request.urlopen
    transport = _Transport(sleep, emit, now)
    owner, name = repo.split("/", 1)
    mine_dir = Path(run_dir) / "mine"
    pr_dir = mine_dir / "prs"
    pr_dir.mkdir(parents=True, exist_ok=True)
    cursor_path = mine_dir / _CURSOR_NAME

    configured = page_size
    cursor, current_size = _load_cursor(cursor_path, configured)
    numbers: list[int] = []
    metrics = {
        "api_requests": 0,
        "graphql_cost_total": 0,
        "overflow_followups": 0,
        "page_size_reductions": 0,
        "duration_s": 0.0,
        "page_size": configured,
    }
    started = now()
    rate = _RateBudget()
    success_streak = 0
    states = _states(filters.state)

    try:
        while True:
            if filters.max_prs is not None and len(numbers) >= filters.max_prs:
                break

            try:
                payload = _graphql(
                    open_url,
                    transport,
                    token,
                    _bulk_query(states is not None),
                    _bulk_variables(owner, name, cursor, current_size, states),
                    metrics,
                    rate,
                    allow_shrink=True,
                )
            except _RetrySmallerPage:
                smaller = _smaller_page_size(current_size)
                if smaller is None:
                    raise ConfigError(
                        "github GraphQL query failed even at page_size=1"
                    ) from None
                current_size = smaller
                metrics["page_size_reductions"] += 1
                success_streak = 0
                continue

            repository = _dig(payload, "data", "repository")
            if repository is None:
                raise ConfigError("repository not found or token cannot see it")
            connection = repository.get("pullRequests")
            if not isinstance(connection, dict):
                raise ConfigError("github pull request page was not an object")

            nodes = connection.get("nodes")
            if not isinstance(nodes, list):
                raise ConfigError("github pull request page was not a list")

            stop = False
            for node in nodes:
                if not isinstance(node, dict):
                    continue
                created = _created_on(node.get("createdAt"))
                if filters.since or filters.until:
                    if created is None or (filters.until and created > filters.until):
                        continue
                    if filters.since and created < filters.since:
                        stop = True
                        break
                if filters.max_prs is not None and len(numbers) >= filters.max_prs:
                    stop = True
                    break

                number = node.get("number")
                if not isinstance(number, int):
                    raise ConfigError("github pull number was not an int")
                updated_at = node.get("updatedAt")
                path = pr_dir / f"{number}.json"
                numbers.append(number)
                if _cache_hit(path, updated_at):
                    continue

                filled = _ensure_complete(
                    node,
                    owner=owner,
                    name=name,
                    number=number,
                    open_url=open_url,
                    transport=transport,
                    token=token,
                    metrics=metrics,
                    rate=rate,
                )
                dump = _node_to_dump(filled, repo=repo)
                dump["updated_at"] = updated_at
                _write_json(path, dump)

            page_info = connection.get("pageInfo") or {}
            has_next = bool(page_info.get("hasNextPage"))
            end_cursor = page_info.get("endCursor")
            if stop or not has_next or not isinstance(end_cursor, str):
                _clear_cursor(cursor_path)
                break

            cursor = end_cursor
            success_streak += 1
            if success_streak >= _SUCCESS_STREAK_TO_GROW and current_size < configured:
                current_size = _larger_page_size(current_size, configured)
                success_streak = 0
            _save_cursor(cursor_path, cursor, current_size)

        _write_json(mine_dir / "output.json", numbers)
    finally:
        metrics["duration_s"] = max(0.0, now() - started)

    return numbers, metrics


class _RetrySmallerPage(Exception):
    """The current page failed in a way that may succeed with fewer nodes."""


class _RateBudget:
    def __init__(self) -> None:
        self.remaining: int | None = None
        self.reset_at: str | None = None
        self.last_cost: int = 1


def _bulk_query(with_states: bool) -> str:
    if with_states:
        signature = (
            "query($owner:String!, $name:String!, $cursor:String, "
            "$pageSize:Int!, $states:[PullRequestState!])"
        )
        pull_args = (
            "first:$pageSize, after:$cursor, states:$states, "
            "orderBy:{field:CREATED_AT, direction:DESC}"
        )
    else:
        signature = (
            "query($owner:String!, $name:String!, $cursor:String, $pageSize:Int!)"
        )
        pull_args = (
            "first:$pageSize, after:$cursor, "
            "orderBy:{field:CREATED_AT, direction:DESC}"
        )
    return f"""
{signature} {{
  repository(owner:$owner, name:$name) {{
    pullRequests({pull_args}) {{
      pageInfo {{ hasNextPage endCursor }}
      nodes {{
        id number title body state merged createdAt updatedAt
        baseRepository {{ nameWithOwner }}
        comments(first:{_COMMENTS_PAGE}) {{
          pageInfo {{ hasNextPage endCursor }}
          nodes {{ body createdAt }}
        }}
        reviewThreads(first:{_THREADS_PAGE}) {{
          pageInfo {{ hasNextPage endCursor }}
          nodes {{
            id path
            comments(first:{_THREAD_COMMENTS_PAGE}) {{
              pageInfo {{ hasNextPage endCursor }}
              nodes {{ body line startLine createdAt }}
            }}
          }}
        }}
      }}
    }}
  }}
  rateLimit {{ cost remaining resetAt }}
}}
""".strip()


_OVERFLOW_QUERY = f"""
query(
  $owner:String!
  $name:String!
  $number:Int!
  $fetchComments:Boolean!
  $commentsCursor:String
  $fetchThreads:Boolean!
  $threadsCursor:String
) {{
  repository(owner:$owner, name:$name) {{
    pullRequest(number:$number) {{
      comments(first:{_COMMENTS_PAGE}, after:$commentsCursor) @include(if:$fetchComments) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{ body createdAt }}
      }}
      reviewThreads(first:{_THREADS_PAGE}, after:$threadsCursor) @include(if:$fetchThreads) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{
          id path
          comments(first:{_THREAD_COMMENTS_PAGE}) {{
            pageInfo {{ hasNextPage endCursor }}
            nodes {{ body line startLine createdAt }}
          }}
        }}
      }}
    }}
  }}
  rateLimit {{ cost remaining resetAt }}
}}
""".strip()

_THREAD_COMMENTS_QUERY = f"""
query($id:ID!, $cursor:String) {{
  node(id:$id) {{
    ... on PullRequestReviewThread {{
      comments(first:{_THREAD_COMMENTS_PAGE}, after:$cursor) {{
        pageInfo {{ hasNextPage endCursor }}
        nodes {{ body line startLine createdAt }}
      }}
    }}
  }}
  rateLimit {{ cost remaining resetAt }}
}}
""".strip()


def _bulk_variables(
    owner: str,
    name: str,
    cursor: str | None,
    page_size: int,
    states: list[str] | None,
) -> dict[str, Any]:
    variables: dict[str, Any] = {
        "owner": owner,
        "name": name,
        "cursor": cursor,
        "pageSize": page_size,
    }
    if states is not None:
        variables["states"] = states
    return variables


def _states(state: str) -> list[str] | None:
    if state == "open":
        return ["OPEN"]
    if state == "closed":
        # REST ``state=closed`` includes merged pulls; GraphQL splits MERGED out.
        return ["CLOSED", "MERGED"]
    return None


def _node_to_dump(node: dict[str, Any], *, repo: str) -> dict[str, Any]:
    base = node.get("baseRepository")
    if isinstance(base, dict) and isinstance(base.get("nameWithOwner"), str):
        repository = base["nameWithOwner"]
    else:
        repository = repo
    comments = _connection_nodes(node.get("comments"))
    threads = _connection_nodes(node.get("reviewThreads"))
    return {
        "base_repository": repository,
        "id": node.get("id"),
        "number": node.get("number"),
        "title": node.get("title"),
        "state": node.get("state"),
        "merged": node.get("merged"),
        "body": node.get("body"),
        "timeline_items": _timeline_items(comments, threads),
    }


def _timeline_items(
    comments: list[dict[str, Any]],
    threads: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    entries: list[tuple[tuple[int, float, str], int, dict[str, Any]]] = []
    for comment in comments:
        item = {
            "__typename": "IssueComment",
            "body": comment.get("body"),
            "created_at": comment.get("createdAt"),
        }
        entries.append((_order_key(_timestamp(comment.get("createdAt"))), len(entries), item))
    for thread in threads:
        thread_comments = _connection_nodes(thread.get("comments"))
        if not thread_comments and isinstance(thread.get("comments"), list):
            thread_comments = [c for c in thread["comments"] if isinstance(c, dict)]
        mapped = [
            {
                "body": comment.get("body"),
                "line": comment.get("line"),
                "start_line": comment.get("startLine", comment.get("start_line")),
                "created_at": comment.get("createdAt", comment.get("created_at")),
            }
            for comment in thread_comments
        ]
        stamps = [_timestamp(comment.get("created_at")) for comment in mapped]
        stamps = [stamp for stamp in stamps if stamp]
        created = min(stamps, key=_order_key) if stamps else ""
        item = {
            "__typename": "PullRequestReviewThread",
            "path": thread.get("path"),
            "comments": mapped,
        }
        entries.append((_order_key(created), len(entries), item))
    entries.sort(key=lambda entry: (entry[0], entry[1]))
    return [item for _, _, item in entries]


def _ensure_complete(
    node: dict[str, Any],
    *,
    owner: str,
    name: str,
    number: int,
    open_url: Any,
    transport: _Transport,
    token: str,
    metrics: dict[str, Any],
    rate: _RateBudget,
) -> dict[str, Any]:
    """Page overflowing nested connections for one pull request."""
    comments = dict(node.get("comments") or {})
    threads = dict(node.get("reviewThreads") or {})
    comment_nodes = list(_connection_nodes(comments))
    thread_nodes = [dict(thread) for thread in _connection_nodes(threads)]

    comments_info = comments.get("pageInfo") or {}
    threads_info = threads.get("pageInfo") or {}
    comments_cursor = comments_info.get("endCursor")
    threads_cursor = threads_info.get("endCursor")
    need_comments = bool(comments_info.get("hasNextPage"))
    need_threads = bool(threads_info.get("hasNextPage"))

    while need_comments or need_threads:
        payload = _graphql(
            open_url,
            transport,
            token,
            _OVERFLOW_QUERY,
            {
                "owner": owner,
                "name": name,
                "number": number,
                "fetchComments": need_comments,
                "commentsCursor": comments_cursor if need_comments else None,
                "fetchThreads": need_threads,
                "threadsCursor": threads_cursor if need_threads else None,
            },
            metrics,
            rate,
            overflow=True,
            allow_shrink=False,
        )
        pull = _dig(payload, "data", "repository", "pullRequest")
        if not isinstance(pull, dict):
            raise ConfigError("github overflow pull request was not an object")
        if need_comments:
            page = pull.get("comments") or {}
            comment_nodes.extend(_connection_nodes(page))
            info = page.get("pageInfo") or {}
            need_comments = bool(info.get("hasNextPage"))
            comments_cursor = info.get("endCursor")
        if need_threads:
            page = pull.get("reviewThreads") or {}
            thread_nodes.extend(dict(thread) for thread in _connection_nodes(page))
            info = page.get("pageInfo") or {}
            need_threads = bool(info.get("hasNextPage"))
            threads_cursor = info.get("endCursor")

    for index, thread in enumerate(thread_nodes):
        thread_nodes[index] = _complete_thread_comments(
            thread,
            open_url=open_url,
            transport=transport,
            token=token,
            metrics=metrics,
            rate=rate,
        )

    filled = dict(node)
    filled["comments"] = {"nodes": comment_nodes, "pageInfo": {"hasNextPage": False}}
    filled["reviewThreads"] = {
        "nodes": thread_nodes,
        "pageInfo": {"hasNextPage": False},
    }
    return filled


def _complete_thread_comments(
    thread: dict[str, Any],
    *,
    open_url: Any,
    transport: _Transport,
    token: str,
    metrics: dict[str, Any],
    rate: _RateBudget,
) -> dict[str, Any]:
    comments = dict(thread.get("comments") or {})
    nodes = list(_connection_nodes(comments))
    info = comments.get("pageInfo") or {}
    if not info.get("hasNextPage"):
        filled = dict(thread)
        filled["comments"] = {"nodes": nodes, "pageInfo": {"hasNextPage": False}}
        return filled

    thread_id = thread.get("id")
    if not isinstance(thread_id, str):
        raise ConfigError("github review thread id was missing during overflow")
    cursor = info.get("endCursor")
    while True:
        payload = _graphql(
            open_url,
            transport,
            token,
            _THREAD_COMMENTS_QUERY,
            {"id": thread_id, "cursor": cursor},
            metrics,
            rate,
            overflow=True,
            allow_shrink=False,
        )
        page = _dig(payload, "data", "node", "comments")
        if not isinstance(page, dict):
            raise ConfigError("github thread comment page was not an object")
        nodes.extend(_connection_nodes(page))
        info = page.get("pageInfo") or {}
        if not info.get("hasNextPage"):
            break
        cursor = info.get("endCursor")

    filled = dict(thread)
    filled["comments"] = {"nodes": nodes, "pageInfo": {"hasNextPage": False}}
    return filled


def _graphql(
    opener: Any,
    transport: _Transport,
    token: str,
    query: str,
    variables: dict[str, Any],
    metrics: dict[str, Any],
    rate: _RateBudget,
    *,
    overflow: bool = False,
    allow_shrink: bool = False,
) -> dict[str, Any]:
    _respect_budget(rate, transport)
    body = json.dumps({"query": query, "variables": variables}).encode()
    request = urllib.request.Request(
        _GRAPHQL_URL,
        data=body,
        method="POST",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "asgard",
        },
    )
    while True:
        try:
            with opener(request, timeout=60) as response:
                raw = response.read().decode()
                headers = getattr(response, "headers", None)
        except urllib.error.HTTPError as exc:
            headers = getattr(exc, "headers", None)
            if exc.code in (403, 429) and _pause(headers, transport, on_error=True):
                exc.close()
                continue
            if exc.code in (502, 504) and allow_shrink:
                exc.close()
                raise _RetrySmallerPage from None
            raise ConfigError(_github_error(exc, token)) from None

        _pause(headers, transport, on_error=False)
        metrics["api_requests"] += 1
        if overflow:
            metrics["overflow_followups"] += 1

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ConfigError("github GraphQL response was not JSON") from exc
        if not isinstance(payload, dict):
            raise ConfigError("github GraphQL response was not an object")

        _note_rate_limit(_dig(payload, "data", "rateLimit"), rate, metrics)

        errors = payload.get("errors")
        if errors:
            # Never accept partial data with errors. Shrink the bulk page when
            # allowed; otherwise surface the GraphQL message.
            if allow_shrink:
                raise _RetrySmallerPage
            raise ConfigError(_graphql_error_message(errors, token))
        if "data" not in payload or payload.get("data") is None:
            if allow_shrink:
                raise _RetrySmallerPage
            raise ConfigError("github GraphQL response had no data")
        return payload


def _note_rate_limit(rate_limit: Any, rate: _RateBudget, metrics: dict[str, Any]) -> None:
    if not isinstance(rate_limit, dict):
        return
    cost = rate_limit.get("cost")
    if isinstance(cost, int) and not isinstance(cost, bool):
        metrics["graphql_cost_total"] += cost
        rate.last_cost = max(1, cost)
    remaining = rate_limit.get("remaining")
    if isinstance(remaining, int) and not isinstance(remaining, bool):
        rate.remaining = remaining
    reset_at = rate_limit.get("resetAt")
    if isinstance(reset_at, str) and reset_at:
        rate.reset_at = reset_at


def _respect_budget(rate: _RateBudget, transport: _Transport) -> None:
    if rate.remaining is None or rate.reset_at is None:
        return
    if rate.remaining >= rate.last_cost:
        return
    wait = _seconds_until(rate.reset_at, transport.now())
    if wait is None or wait <= 0:
        return
    transport.emit(RateLimited(wait_seconds=wait))
    transport.sleep(wait)
    rate.remaining = None


def _seconds_until(reset_at: str, now: float) -> float | None:
    normalized = reset_at[:-1] + "+00:00" if reset_at.endswith("Z") else reset_at
    try:
        target = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    return max(0.0, target.timestamp() - now)


def _graphql_error_message(errors: Any, token: str) -> str:
    if isinstance(errors, list) and errors:
        first = errors[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, str) and message.strip():
                text = f"github GraphQL error: {message.strip()}"
                return text.replace(token, "***") if token else text
    return "github GraphQL error"


def _load_cursor(path: Path, configured: int) -> tuple[str | None, int]:
    if not path.is_file():
        return None, configured
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None, configured
    if not isinstance(payload, dict):
        return None, configured
    cursor = payload.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        cursor = None
    size = payload.get("page_size")
    if not isinstance(size, int) or isinstance(size, bool) or size < 1:
        size = configured
    else:
        size = min(size, configured)
    return cursor, size


def _save_cursor(path: Path, cursor: str, page_size: int) -> None:
    _write_json(path, {"cursor": cursor, "page_size": page_size})


def _clear_cursor(path: Path) -> None:
    if path.is_file():
        path.unlink()


def _smaller_page_size(current: int) -> int | None:
    for step in _PAGE_LADDER:
        if step < current:
            return step
    if current > 1:
        return max(1, current // 2)
    return None


def _larger_page_size(current: int, configured: int) -> int:
    if current >= configured:
        return configured
    candidates = [step for step in _PAGE_LADDER if current < step <= configured]
    if candidates:
        return min(candidates)
    return configured


def _connection_nodes(connection: Any) -> list[dict[str, Any]]:
    if not isinstance(connection, dict):
        return []
    nodes = connection.get("nodes")
    if not isinstance(nodes, list):
        return []
    return [node for node in nodes if isinstance(node, dict)]


def _dig(payload: Any, *keys: str) -> Any:
    current = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _created_on(value: Any) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _timestamp(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        dated = value.get("$date")
        if isinstance(dated, str):
            return dated
    return ""


def _order_key(timestamp: str) -> tuple[int, float, str]:
    if not timestamp:
        return (1, 0.0, "")
    normalized = timestamp[:-1] + "+00:00" if timestamp.endswith("Z") else timestamp
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return (0, 0.0, timestamp)
    return (0, parsed.timestamp(), timestamp)
