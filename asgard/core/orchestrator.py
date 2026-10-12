"""Runs pipeline stages in order and forwards their events to sinks."""

from contextlib import ExitStack
from pathlib import Path
from typing import Any

from asgard.core.events import Event
from asgard.core.sinks import Sink
from asgard.core.stage import RunContext, Stage


class Orchestrator:
    def __init__(self, stages: list[Stage], sinks: list[Sink] | None = None) -> None:
        self.stages = stages
        self.sinks = sinks or []

    def emit(self, event: Event) -> None:
        for sink in self.sinks:
            sink.handle(event)

    def run(self, run_dir: Path, *, only: str | None = None, data: Any = None) -> Any:
        run_dir.mkdir(parents=True, exist_ok=True)
        selected = self.stages
        previous: Stage | None = None
        if only is not None:
            names = [stage.name for stage in self.stages]
            if only not in names:
                raise ValueError(f"unknown stage: {only}")
            index = names.index(only)
            selected = [self.stages[index]]
            if index > 0:
                previous = self.stages[index - 1]
        with ExitStack() as stack:
            for sink in self.sinks:
                if hasattr(sink, "__enter__"):
                    stack.enter_context(sink)
            ctx = RunContext(run_dir, self.emit)
            if data is None and previous is not None and previous.is_done(ctx):
                data = previous.load(ctx)
            for stage in selected:
                data = stage.run(ctx, data)
        return data
