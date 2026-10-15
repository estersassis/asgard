"""Pretty printer for a run's progress log."""

import json
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from rich.console import Console
from rich.text import Text

from asgard.core.config import ConfigError
from asgard.core.normalization import NormalizedPullRequest
from asgard.core.stages.classify import batch_places

_POLL_S = 0.2
_ITEMS = {
    "PRMined",
    "PRFiltered",
    "ClassificationAttempt",
    "EvaluatorVerdict",
    "LoopFinished",
    "SynthesisChunk",
}


def show_logs(
    run_dir: Path,
    *,
    follow: bool = False,
    stage: str | None = None,
    console: Console | None = None,
    pause: Callable[[float], None] = time.sleep,
) -> None:
    """Print ``events.jsonl``, grouped by stage. Follow keeps reading until interrupted."""
    path = run_dir / "events.jsonl"
    if not path.is_file():
        raise ConfigError("events.jsonl is missing")
    output = console or Console()
    try:
        _stream(path, output, stage=stage, follow=follow, pause=pause)
    except KeyboardInterrupt:
        return


def _stream(
    path: Path,
    console: Console,
    *,
    stage: str | None,
    follow: bool,
    pause: Callable[[float], None],
) -> None:
    tail = _Tail(path)
    view = _View(console, stage, path.parent)
    for record in tail.read():
        view.handle(record)
    while follow:
        pause(_POLL_S)
        for record in tail.read():
            view.handle(record)


class _Tail:
    """Complete lines only. A trailing fragment waits for its newline."""

    def __init__(self, path: Path) -> None:
        self._handle = path.open(encoding="utf-8")
        self._pending = ""

    def read(self) -> list[dict[str, Any]]:
        chunk = self._handle.read()
        if not chunk:
            return []
        self._pending += chunk
        lines = self._pending.split("\n")
        self._pending = lines.pop()
        records: list[dict[str, Any]] = []
        for line in lines:
            if not line.strip():
                continue
            records.append(_record(line))
        return records


def _record(line: str) -> dict[str, Any]:
    try:
        payload = json.loads(line)
    except json.JSONDecodeError:
        raise ConfigError("events.jsonl is not valid json") from None
    if not isinstance(payload, dict):
        raise ConfigError("events.jsonl is not valid json")
    return payload


class _Cursor:
    def __init__(self, kind: str, index: int, total: int) -> None:
        self.kind = kind
        self.index = index
        self.total = total

    def text(self) -> str:
        if self.kind == "batch":
            return f"batch {self.index}/{self.total}"
        return f"pr {self.index}/{self.total}"


class _View:
    def __init__(self, console: Console, stage: str | None, run_dir: Path) -> None:
        self.console = console
        self.only = stage
        self.run_dir = run_dir
        self.stage: str | None = None
        self.started_at: str | None = None
        self.items = 0
        self.visible = stage is None
        self.cursor: _Cursor | None = None
        self.saw_progress = False
        self.inferred: dict[str, str] | None = None

    def handle(self, record: dict[str, Any]) -> None:
        kind = record.get("type")
        if kind == "StageStarted":
            self._open(record)
            return
        if kind == "StageFinished":
            self._close(record)
            return
        if kind == "ClassifyProgress":
            self._progress(record)
            return
        if not self.visible:
            return
        if kind in _ITEMS:
            self.items += 1
        label, detail, style = _describe(record)
        _emit(self.console, record.get("at"), label, self._with_place(record, detail), style)

    def _progress(self, record: dict[str, Any]) -> None:
        if self.stage != "classify":
            return
        cursor = _cursor(record)
        if cursor is None:
            return
        self.saw_progress = True
        self.cursor = cursor
        if not self.visible:
            return
        label = "batch" if cursor.kind == "batch" else "pr"
        _emit(self.console, record.get("at"), label, f"{cursor.index}/{cursor.total}", "bold")

    def _with_place(self, record: dict[str, Any], detail: str) -> str:
        if self.stage != "classify":
            return detail
        place = self._place(record.get("pr_id"))
        if not place:
            return detail
        return f"{place}  {detail}" if detail else place

    def _place(self, pr_id: Any) -> str:
        if self.saw_progress and self.cursor is not None:
            return self.cursor.text()
        if self.saw_progress or not isinstance(pr_id, str):
            return ""
        if self.inferred is None:
            self.inferred = _inferred_places(self.run_dir)
        return self.inferred.get(pr_id, "")

    def _open(self, record: dict[str, Any]) -> None:
        self.stage = _name(record.get("stage"))
        self.started_at = _stamp(record.get("at"))
        self.items = 0
        self.cursor = None
        self.saw_progress = False
        self.inferred = None
        self.visible = self.only is None or self.stage == self.only
        if not self.visible:
            return
        self.console.rule(self.stage or "stage", style="cyan")
        total = record.get("total")
        detail = ""
        if isinstance(total, int) and not isinstance(total, bool):
            detail = f"{total} pull requests"
        _emit(self.console, record.get("at"), "started", detail, "bold")

    def _close(self, record: dict[str, Any]) -> None:
        if self.stage is None:
            self.stage = _name(record.get("stage"))
            self.started_at = None
            self.items = 0
            self.visible = self.only is None or self.stage == self.only
            if self.visible:
                self.console.rule(self.stage or "stage", style="cyan")
        if not self.visible:
            self._reset()
            return
        skipped = record.get("skipped") is True
        _emit(
            self.console,
            record.get("at"),
            "skipped" if skipped else "finished",
            _finish_detail(self.items, self.started_at, record.get("at")),
            "dim" if skipped else "bold",
        )
        self._reset()

    def _reset(self) -> None:
        self.stage = None
        self.started_at = None
        self.items = 0
        self.visible = self.only is None


def _cursor(record: dict[str, Any]) -> _Cursor | None:
    kind = record.get("kind")
    index = record.get("index")
    total = record.get("total")
    if kind not in ("batch", "pull"):
        return None
    if isinstance(index, bool) or not isinstance(index, int) or index < 1:
        return None
    if isinstance(total, bool) or not isinstance(total, int) or total < 1:
        return None
    return _Cursor(kind, index, total)


def _inferred_places(run_dir: Path) -> dict[str, str]:
    """Place labels for a classify log that predates ClassifyProgress events."""
    config, pulls = _run_pulls(run_dir)
    if config is None or not pulls:
        return {}
    batch_size = _positive(config.get("batch_size"), default=1)
    if batch_size == 1:
        total = len(pulls)
        return {
            pull.context.pr_id: f"pr {index}/{total}"
            for index, pull in enumerate(pulls, start=1)
        }
    places = batch_places(pulls, batch_size, _optional_positive(config.get("batch_max_chars")))
    return {pr_id: f"batch {index}/{total}" for pr_id, (index, total) in places.items()}


def _run_pulls(run_dir: Path) -> tuple[dict[str, Any] | None, list[NormalizedPullRequest]]:
    try:
        config = json.loads((run_dir / "config.json").read_text(encoding="utf-8"))
        raw = json.loads((run_dir / "normalize" / "output.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, []
    if not isinstance(config, dict) or not isinstance(raw, list):
        return None, []
    pulls: list[NormalizedPullRequest] = []
    seen: set[str] = set()
    for item in raw:
        try:
            pull = NormalizedPullRequest.model_validate(item)
        except ValidationError:
            return None, []
        pr_id = pull.context.pr_id
        if pr_id in seen:
            continue
        seen.add(pr_id)
        pulls.append(pull)
    return config, pulls


def _positive(value: Any, *, default: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return value


def _optional_positive(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def _describe(record: dict[str, Any]) -> tuple[str, str, str]:
    kind = record.get("type")
    if kind == "PRMined":
        return "mined", _text(record.get("pr_id")), "cyan"
    if kind == "ClassificationAttempt":
        return "classified", _text(record.get("pr_id")), "cyan"
    if kind == "LoopFinished":
        return "loop finished", _text(record.get("pr_id")), "cyan"
    if kind == "EvaluatorVerdict":
        if record.get("approved") is True:
            return "approved", _text(record.get("pr_id")), "green"
        return "revise", _text(record.get("pr_id")), "yellow"
    if kind == "PRFiltered":
        return "filtered", _pair(record.get("pr_id"), record.get("reason")), "dim"
    if kind == "RateLimited":
        return "waiting", _seconds(record.get("wait_seconds")), "yellow"
    if kind == "CitationWarning":
        return "warning", _text(record.get("message")), "yellow"
    if kind == "Error":
        return "error", _text(record.get("message")), "red"
    if kind == "SynthesisChunk":
        text = record.get("text")
        count = len(text) if isinstance(text, str) else 0
        return "synthesis", f"guideline draft, {count} characters", "cyan"
    return _text(kind) or "event", "", ""


def _emit(console: Console, at: Any, label: str, detail: str, style: str) -> None:
    line = Text()
    line.append(f"{_clock(at):<8}", style="dim")
    line.append("  ")
    line.append(f"{label:<14}", style=style or None)
    if detail:
        line.append("  ")
        line.append(detail)
    console.print(line, highlight=False)


def _finish_detail(items: int, started_at: str | None, finished_at: Any) -> str:
    noun = "event" if items == 1 else "events"
    parts = [f"{items} {noun}"]
    duration = _duration(started_at, finished_at)
    if duration is not None:
        parts.append(duration)
    return " · ".join(parts)


def _duration(started_at: str | None, finished_at: Any) -> str | None:
    if started_at is None or not isinstance(finished_at, str):
        return None
    try:
        delta = datetime.fromisoformat(finished_at) - datetime.fromisoformat(started_at)
    except ValueError:
        return None
    seconds = max(0, int(delta.total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if hours or minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


def _clock(at: Any) -> str:
    stamp = _stamp(at)
    if stamp is None:
        return " " * 8
    try:
        return datetime.fromisoformat(stamp).strftime("%H:%M:%S")
    except ValueError:
        return " " * 8


def _stamp(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _name(value: Any) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return ""


def _pair(left: Any, right: Any) -> str:
    parts = [part for part in (_text(left), _text(right)) if part]
    return "  ".join(parts)


def _seconds(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return ""
    return f"{value:g}s"
