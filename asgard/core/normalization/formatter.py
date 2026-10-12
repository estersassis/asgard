"""Extract the canonical pull request document from a GraphQL dump."""

from typing import Any

from asgard.core.normalization.schema import SCHEMA_VERSION


def format_dump(payload: dict[str, Any]) -> dict[str, Any]:
    """Map a GraphQL-shaped pull request onto the canonical document.

    General comments come only from ``IssueComment`` bodies that are non-empty
    after stripping. Review threads come only from ``PullRequestReviewThread``.
    Null bodies become empty strings. A merged pull uses the state ``MERGED``.
    """
    merged = payload.get("merged") is True
    return {
        "schema_version": SCHEMA_VERSION,
        "context": {
            "repository": _repository(payload.get("base_repository")),
            "pr_id": _string(payload.get("id")),
            "title": _string(payload.get("title")),
            "state": "MERGED" if merged else _state(payload.get("state")),
            "is_merged": merged,
        },
        "description": _string(payload.get("body")),
        "general_discussion": _general_discussion(payload.get("timeline_items")),
        "code_review_threads": _threads(payload.get("timeline_items")),
    }


def _repository(value: Any) -> str:
    if isinstance(value, str) and value:
        return value
    return "unknown"


def _string(value: Any) -> str:
    if isinstance(value, str):
        return value
    return ""


def _state(value: Any) -> str:
    if isinstance(value, str):
        return value.upper()
    return ""


def _general_discussion(items: Any) -> list[str]:
    discussion: list[str] = []
    for item in _items(items):
        if item.get("__typename") != "IssueComment":
            continue
        body = _string(item.get("body")).strip()
        if body:
            discussion.append(body)
    return discussion


def _threads(items: Any) -> list[dict[str, Any]]:
    threads: list[dict[str, Any]] = []
    for item in _items(items):
        if item.get("__typename") != "PullRequestReviewThread":
            continue
        comments = _thread_comments(item.get("comments"))
        if not comments:
            continue
        threads.append({"scope": _scope(item), "comments": comments})
    return threads


def _thread_comments(comments: Any) -> list[str]:
    if not isinstance(comments, list):
        return []
    bodies: list[str] = []
    for comment in comments:
        if not isinstance(comment, dict):
            continue
        body = _string(comment.get("body")).strip()
        if body:
            bodies.append(body)
    return bodies


def _scope(item: dict[str, Any]) -> str:
    path = item.get("path") or "unknown_file"
    raw_comments = item.get("comments")
    first = raw_comments[0] if isinstance(raw_comments, list) and raw_comments else {}
    if not isinstance(first, dict):
        first = {}
    line = first.get("line") or first.get("start_line")
    scope = f"FILE:{path}"
    if line:
        return f"{scope}#L{line}"
    return scope


def _items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]
