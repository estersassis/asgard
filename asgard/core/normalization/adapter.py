"""Project a mined GitHub REST pull into the GraphQL dump shape."""

from datetime import datetime
from typing import Any

from asgard.core.normalization.formatter import format_dump


def format_pull(payload: dict[str, Any], *, repo: str | None = None) -> dict[str, Any]:
    """Format a GraphQL dump, or adapt a REST pull and then format it.

    A payload that already has ``timeline_items`` is a dump. Anything else is
    the REST document written by the mine stage.
    """
    if "timeline_items" in payload:
        return format_dump(payload)
    return format_dump(rest_to_dump(payload, repo=repo))


def rest_to_dump(payload: dict[str, Any], *, repo: str | None = None) -> dict[str, Any]:
    """Project REST pull JSON onto the keys the formatter reads.

    ``base_repository`` comes from ``base.repo.full_name``, then the run repo.
    ``id`` comes from ``node_id``. Issue comments and review threads share one
    timeline ordered by ``created_at``. Review bodies that are not inside a
    thread are left out.
    """
    return {
        "base_repository": _repository(payload, repo),
        "id": _string(payload.get("node_id")),
        "title": payload.get("title"),
        "state": payload.get("state"),
        "merged": payload.get("merged"),
        "body": payload.get("body"),
        "timeline_items": _timeline(payload),
    }


def _repository(payload: dict[str, Any], repo: str | None) -> str:
    base = payload.get("base")
    if isinstance(base, dict):
        base_repo = base.get("repo")
        if isinstance(base_repo, dict):
            full_name = base_repo.get("full_name")
            if isinstance(full_name, str) and full_name:
                return full_name
    if isinstance(repo, str) and repo:
        return repo
    return "unknown"


def _timeline(payload: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[tuple[tuple[int, float, str], int, dict[str, Any]]] = []
    for comment in _dicts(payload.get("issue_comments")):
        entries.append((_order_key(_created_at(comment)), len(entries), _issue_comment(comment)))
    for thread in _dicts(payload.get("threads")):
        comments = _dicts(thread.get("comments"))
        entries.append(
            (_order_key(_thread_created_at(comments)), len(entries), _thread(thread, comments))
        )
    entries.sort(key=lambda entry: (entry[0], entry[1]))
    return [item for _, _, item in entries]


def _issue_comment(comment: dict[str, Any]) -> dict[str, Any]:
    return {"__typename": "IssueComment", "body": comment.get("body")}


def _thread(thread: dict[str, Any], comments: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "__typename": "PullRequestReviewThread",
        "path": _thread_path(thread, comments),
        "comments": [_review_comment(comment) for comment in comments],
    }


def _review_comment(comment: dict[str, Any]) -> dict[str, Any]:
    return {
        "body": comment.get("body"),
        "line": comment.get("line"),
        "start_line": comment.get("start_line"),
    }


def _thread_path(thread: dict[str, Any], comments: list[dict[str, Any]]) -> str:
    path = thread.get("path")
    if isinstance(path, str) and path:
        return path
    for comment in comments:
        comment_path = comment.get("path")
        if isinstance(comment_path, str) and comment_path:
            return comment_path
    return "unknown_file"


def _thread_created_at(comments: list[dict[str, Any]]) -> str:
    stamps = [_created_at(comment) for comment in comments]
    stamps = [stamp for stamp in stamps if stamp]
    if not stamps:
        return ""
    return min(stamps, key=_order_key)


def _created_at(value: dict[str, Any]) -> str:
    raw = value.get("created_at")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        dated = raw.get("$date")
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


def _dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _string(value: Any) -> str:
    if isinstance(value, str):
        return value
    return ""
