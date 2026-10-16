"""Forward orchestrator events to SSE clients."""

import queue
from typing import Any

from asgard.core.events import Event
from asgard.core.sinks import event_record

_SENTINEL = object()


class UISink:
    """Thread-safe sink: each ``handle`` enqueues one event record for SSE."""

    def __init__(self) -> None:
        self._queue: queue.Queue[dict[str, Any] | object] = queue.Queue()

    def handle(self, event: Event) -> None:
        self._queue.put(event_record(event))

    def close(self) -> None:
        self._queue.put(_SENTINEL)

    def get(self, timeout: float | None = None) -> dict[str, Any] | None:
        """Return the next event, or ``None`` when the run has finished."""
        item = self._queue.get(timeout=timeout)
        if item is _SENTINEL:
            return None
        return item  # type: ignore[return-value]

    def drain(self) -> list[dict[str, Any]]:
        """Collect all queued events without waiting for close."""
        events: list[dict[str, Any]] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break
            if item is _SENTINEL:
                break
            events.append(item)  # type: ignore[arg-type]
        return events
