"""Runs pipeline stages in order and forwards their events to sinks."""

from collections.abc import Callable
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

    def run(
        self,
        run_dir: Path,
        *,
        only: str | None = None,
        start: str | None = None,
        data: Any = None,
        cancel: Callable[[], bool] | None = None,
    ) -> Any:
        """Run the pipeline, one stage, or from ``start`` through the end.

        ``only`` runs that stage alone. ``start`` runs that stage and every
        stage after it. Either way, each earlier stage must already have
        written its output; that output is the input when ``data`` is omitted.
        """
        if only is not None and start is not None:
            raise ValueError("pass only one of only and start")
        run_dir.mkdir(parents=True, exist_ok=True)
        names = [stage.name for stage in self.stages]
        selected = self.stages
        index = 0
        chosen = only if only is not None else start
        if chosen is not None:
            if chosen not in names:
                raise ValueError(f"unknown stage: {chosen}")
            index = names.index(chosen)
            selected = [self.stages[index]] if only is not None else self.stages[index:]
        with ExitStack() as stack:
            for sink in self.sinks:
                if hasattr(sink, "__enter__"):
                    stack.enter_context(sink)
            ctx = RunContext(run_dir, self.emit, cancel=cancel)
            if index > 0:
                pending = next(
                    (stage.name for stage in self.stages[:index] if not stage.is_done(ctx)),
                    None,
                )
                if pending is not None:
                    raise ValueError(f"previous stage is not done: {pending}")
                if data is None:
                    data = self.stages[index - 1].load(ctx)
            for stage in selected:
                data = stage.run(ctx, data)
        return data
