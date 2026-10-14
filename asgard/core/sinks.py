"""Event sinks. A new interface is a new sink, not a change to the stages."""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TextIO

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    TaskID,
    TaskProgressColumn,
    TextColumn,
    TimeElapsedColumn,
)

from asgard.core.events import (
    CitationWarning,
    ClassificationAttempt,
    Error,
    EvaluatorVerdict,
    Event,
    LoopFinished,
    PRFiltered,
    PRMined,
    RateLimited,
    StageFinished,
    StageStarted,
    SynthesisChunk,
)

_ITEM_EVENTS = (
    PRMined,
    PRFiltered,
    ClassificationAttempt,
    EvaluatorVerdict,
    LoopFinished,
    SynthesisChunk,
)


class Sink(Protocol):
    def handle(self, event: Event) -> None:
        """Receive one progress event."""


def event_record(event: Event) -> dict[str, Any]:
    """JSON object for one event, including the UTC timestamp."""
    payload = json.loads(event.model_dump_json())
    payload["at"] = datetime.now(timezone.utc).isoformat()
    return payload


class JSONLSink:
    def __init__(self, path: Path) -> None:
        self.path = path

    def handle(self, event: Event) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event_record(event)) + "\n")


class StreamJSONSink:
    """One JSON event per line, flushed so a CI log can read it immediately."""

    def __init__(self, stream: TextIO) -> None:
        self.stream = stream

    def handle(self, event: Event) -> None:
        self.stream.write(json.dumps(event_record(event)) + "\n")
        self.stream.flush()


class LoggingSink:
    def __init__(self, logger: logging.Logger | None = None) -> None:
        self.logger = logger or logging.getLogger("asgard")

    def handle(self, event: Event) -> None:
        self.logger.info("%s", event.model_dump_json())


class CLIProgressSink:
    def __init__(self, console: Console | None = None) -> None:
        self.progress = Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            TimeElapsedColumn(),
            console=console or Console(stderr=True),
        )
        self._task: TaskID | None = None

    def __enter__(self) -> "CLIProgressSink":
        self.progress.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.progress.stop()

    def handle(self, event: Event) -> None:
        if isinstance(event, StageStarted):
            self._task = self.progress.add_task(event.stage, total=event.total)
        elif isinstance(event, StageFinished):
            self._complete()
        elif isinstance(event, Error):
            self.progress.console.print(event.message)
        elif isinstance(event, CitationWarning):
            self.progress.console.print(event.message)
        elif isinstance(event, RateLimited):
            self.progress.console.print(f"waiting {event.wait_seconds:g}s")
        elif isinstance(event, _ITEM_EVENTS) and self._task is not None:
            self.progress.advance(self._task)

    def _complete(self) -> None:
        if self._task is None:
            return
        task = next(item for item in self.progress.tasks if item.id == self._task)
        total = task.total or task.completed or 1
        self.progress.update(self._task, total=total, completed=total)
        self._task = None
