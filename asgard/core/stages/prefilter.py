"""Drop pull requests that show no cheap security signal.

The stage never decides that a pull request is about security. With the filter
on, it only removes a pull request when the title, description, discussion,
labels, and known file paths all miss the keyword list.
"""

import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from asgard.core.config import ConfigError
from asgard.core.events import PRFiltered
from asgard.core.normalization import NormalizedPullRequest
from asgard.core.stage import RunContext, Stage

_REASON = "no security keyword, label, or sensitive path"
_INPUT = "prefilter stage requires normalized pull requests"


class PrefilterStage(Stage):
    name = "prefilter"

    def __init__(self, prefilter: bool | None = None) -> None:
        self.prefilter = prefilter

    def item_total(self, data: Any) -> int | None:
        """Input size, so StageStarted.total can report the count before filtering."""
        if isinstance(data, list):
            return len(data)
        return None

    def execute(self, ctx: RunContext, data: Any) -> list[dict[str, Any]]:
        pulls = _pulls(data)
        enabled = self.prefilter if self.prefilter is not None else _enabled(ctx)
        keywords = _keywords() if enabled else ()
        # Resolve every decision before emitting, so a bad mined file fails first.
        decisions = [(not enabled) or _matches(ctx, pull, keywords) for pull in pulls]
        kept: list[dict[str, Any]] = []
        removed: list[dict[str, str]] = []
        for pull, keep in zip(pulls, decisions, strict=True):
            if keep:
                kept.append(pull.model_dump())
                continue
            pr_id = pull.context.pr_id
            ctx.emit(PRFiltered(pr_id=pr_id, reason=_REASON))
            removed.append({"pr_id": pr_id, "reason": _REASON})
        _write_removed(ctx, before=len(pulls), after=len(kept), removed=removed)
        return kept


def _pulls(data: Any) -> list[NormalizedPullRequest]:
    if not isinstance(data, list):
        raise ConfigError(_INPUT)
    pulls: list[NormalizedPullRequest] = []
    for item in data:
        if isinstance(item, NormalizedPullRequest):
            pulls.append(item)
            continue
        if not isinstance(item, dict):
            raise ConfigError(_INPUT)
        try:
            pulls.append(NormalizedPullRequest.model_validate(item))
        except ValidationError:
            raise ConfigError(_INPUT) from None
    return pulls


def _enabled(ctx: RunContext) -> bool:
    path = ctx.run_dir / "config.json"
    if not path.is_file():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise ConfigError("run configuration is invalid") from None
    if not isinstance(data, dict):
        raise ConfigError("run configuration is invalid")
    value = data.get("prefilter", False)
    if not isinstance(value, bool):
        raise ConfigError("run configuration is invalid")
    return value


def _keywords() -> tuple[str, ...]:
    text = files("asgard.data").joinpath("security_keywords.txt").read_text(encoding="utf-8")
    found: list[str] = []
    for line in text.splitlines():
        term = line.strip()
        if not term or term.startswith("#"):
            continue
        found.append(term.casefold())
    return tuple(found)


def _matches(ctx: RunContext, pull: NormalizedPullRequest, keywords: tuple[str, ...]) -> bool:
    if any(_contains(text, keywords) for text in _texts(pull)):
        return True
    mined = _mined(ctx, pull.context.pr_id)
    if mined is None:
        return False
    if any(_contains(label, keywords) for label in _labels(mined)):
        return True
    return any(_contains(path, keywords) for path in _paths(mined))


def _texts(pull: NormalizedPullRequest) -> list[str]:
    texts = [pull.context.title, pull.description, *pull.general_discussion]
    for thread in pull.code_review_threads:
        texts.extend(thread.comments)
    return texts


def _mined(ctx: RunContext, pr_id: str) -> dict[str, Any] | None:
    path = ctx.run_dir / "mine" / "prs" / f"{pr_id}.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise ConfigError(f"mined pull {pr_id} was not an object") from None
    if not isinstance(data, dict):
        raise ConfigError(f"mined pull {pr_id} was not an object")
    return data


def _labels(payload: dict[str, Any]) -> list[str]:
    labels = payload.get("labels")
    if not isinstance(labels, list):
        return []
    names: list[str] = []
    for label in labels:
        if isinstance(label, str):
            names.append(label)
        elif isinstance(label, dict):
            name = label.get("name")
            if isinstance(name, str):
                names.append(name)
    return names


def _paths(payload: dict[str, Any]) -> list[str]:
    """File paths the mine stage actually stored.

    Missing ``files`` and thread paths contribute nothing. A pull request is
    not removed because those fields are absent.
    """
    paths: list[str] = []
    files = payload.get("files")
    if isinstance(files, list):
        for item in files:
            if isinstance(item, str):
                paths.append(item)
            elif isinstance(item, dict):
                filename = item.get("filename")
                if isinstance(filename, str):
                    paths.append(filename)
    threads = payload.get("threads")
    if isinstance(threads, list):
        for thread in threads:
            if not isinstance(thread, dict):
                continue
            path = thread.get("path")
            if isinstance(path, str):
                paths.append(path)
            comments = thread.get("comments")
            if not isinstance(comments, list):
                continue
            for comment in comments:
                if not isinstance(comment, dict):
                    continue
                comment_path = comment.get("path")
                if isinstance(comment_path, str):
                    paths.append(comment_path)
    return paths


def _contains(value: str, keywords: tuple[str, ...]) -> bool:
    folded = value.casefold()
    return any(keyword in folded for keyword in keywords)


def _write_removed(
    ctx: RunContext,
    *,
    before: int,
    after: int,
    removed: list[dict[str, str]],
) -> None:
    payload = {"before": before, "after": after, "removed": removed}
    _replace(ctx.run_dir / "prefilter" / "removed.json", json.dumps(payload) + "\n")


def _replace(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)
