"""Stage contract. A stage reads an input, writes its output, and can be run again."""

import json
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from asgard.core.events import Error, Event, StageFinished, StageStarted

STAGE_NAMES = (
    "input",
    "mine",
    "normalize",
    "prefilter",
    "classify",
    "consolidate",
    "synthesize",
    "security_md",
    "bundle",
)


class CancelledError(Exception):
    """The run was stopped cooperatively. Partial outputs are kept for resume."""


class RunContext:
    def __init__(
        self,
        run_dir: Path,
        emit: Callable[[Event], None],
        *,
        cancel: Callable[[], bool] | None = None,
    ) -> None:
        self.run_dir = run_dir
        self.emit = emit
        self._cancel = cancel or (lambda: False)

    def check_cancelled(self) -> None:
        if self._cancel():
            raise CancelledError("run stopped")


class Stage(ABC):
    name: str

    def output_path(self, ctx: RunContext) -> Path:
        return ctx.run_dir / self.name / "output.json"

    def is_done(self, ctx: RunContext) -> bool:
        return self.output_path(ctx).exists()

    def load(self, ctx: RunContext) -> Any:
        return json.loads(self.output_path(ctx).read_text(encoding="utf-8"))

    def save(self, ctx: RunContext, output: Any) -> None:
        path = self.output_path(ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(output), encoding="utf-8")
        temporary.replace(path)

    def _items_path(self, ctx: RunContext) -> Path:
        return ctx.run_dir / self.name / "items.jsonl"

    def _read_items(self, ctx: RunContext) -> list[dict[str, Any]]:
        path = self._items_path(ctx)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]

    def completed_ids(self, ctx: RunContext) -> set[str]:
        return {item["id"] for item in self._read_items(ctx)}

    def save_item(self, ctx: RunContext, item_id: str, payload: Any) -> None:
        path = self._items_path(ctx)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"id": item_id, "payload": payload}) + "\n")

    def process_items(
        self,
        ctx: RunContext,
        items: Iterable[tuple[str, Any]],
        fn: Callable[[str, Any], Any],
    ) -> list[Any]:
        done = self.completed_ids(ctx)
        outputs = [item["payload"] for item in self._read_items(ctx)]
        for item_id, item in items:
            if item_id in done:
                continue
            ctx.check_cancelled()
            result = fn(item_id, item)
            self.save_item(ctx, item_id, result)
            outputs.append(result)
            done.add(item_id)
        return outputs

    def item_total(self, data: Any) -> int | None:
        """Input size for StageStarted.total. Stages that do not count return None."""
        return None

    @abstractmethod
    def execute(self, ctx: RunContext, data: Any) -> Any:
        """Do the work. Skip ids already stored by process_items."""

    def run(self, ctx: RunContext, data: Any = None) -> Any:
        if self.is_done(ctx):
            ctx.emit(StageFinished(stage=self.name, skipped=True))
            return self.load(ctx)
        ctx.check_cancelled()
        ctx.emit(StageStarted(stage=self.name, total=self.item_total(data)))
        try:
            output = self.execute(ctx, data)
        except Exception as exc:
            ctx.emit(Error(message=str(exc), stage=self.name))
            raise
        self.save(ctx, output)
        ctx.emit(StageFinished(stage=self.name))
        return output
