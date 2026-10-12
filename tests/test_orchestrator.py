"""Orchestrator, stage contract, and progress events."""

import ast
import json
import logging
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from asgard.core.events import (
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
from asgard.core.orchestrator import Orchestrator
from asgard.core.sinks import CLIProgressSink, JSONLSink, LoggingSink
from asgard.core.stage import STAGE_NAMES, RunContext, Stage

CORE_ROOT = Path(__file__).resolve().parents[1] / "asgard" / "core"


class Record(Stage):
    def __init__(self, name: str, log: list[str]) -> None:
        self.name = name
        self.log = log

    def execute(self, ctx: RunContext, data: Any) -> Any:
        self.log.append(self.name)
        return {"stage": self.name, "prev": data}


class Classify(Stage):
    name = "classify"

    def __init__(self, fail_on: str | None = None) -> None:
        self.fail_on = fail_on
        self.seen: list[str] = []

    def execute(self, ctx: RunContext, data: Any) -> Any:
        def work(item_id: str, item: str) -> str:
            self.seen.append(item_id)
            if item_id == self.fail_on:
                raise RuntimeError("killed")
            ctx.emit(ClassificationAttempt(pr_id=item_id))
            return item

        return self.process_items(ctx, [(pr, pr) for pr in data], work)


class ListSink:
    def __init__(self) -> None:
        self.events: list[Event] = []

    def handle(self, event: Event) -> None:
        self.events.append(event)


def test_full_pipeline_single_stage_and_resume(tmp_path: Path) -> None:
    log: list[str] = []
    stages = [Record(name, log) for name in STAGE_NAMES]
    sink = ListSink()
    orchestrator = Orchestrator(stages, [sink])

    result = orchestrator.run(tmp_path)
    assert log == list(STAGE_NAMES)
    assert result["stage"] == "bundle"
    assert result["prev"]["stage"] == "security_md"

    sink.events.clear()
    assert orchestrator.run(tmp_path) == result
    assert log == list(STAGE_NAMES)
    assert [event.stage for event in sink.events] == list(STAGE_NAMES)
    assert all(isinstance(event, StageFinished) and event.skipped for event in sink.events)

    only_log: list[str] = []
    only_dir = tmp_path / "only"
    only = Orchestrator([Record(name, only_log) for name in ("input", "mine")])
    only.run(only_dir, only="input")
    only_log.clear()
    mined = only.run(only_dir, only="mine")
    assert only_log == ["mine"]
    assert mined["prev"]["stage"] == "input"

    with pytest.raises(ValueError, match="unknown stage"):
        only.run(only_dir, only="missing")


def test_resume_skips_finished_prs(tmp_path: Path) -> None:
    prs = ["1", "2", "3"]
    sink = ListSink()
    first = Classify(fail_on="2")
    with pytest.raises(RuntimeError, match="killed"):
        Orchestrator([first], [sink]).run(tmp_path, data=prs)
    assert first.seen == ["1", "2"]
    assert not (tmp_path / "classify" / "output.json").exists()
    assert any(isinstance(event, Error) for event in sink.events)

    second = Classify()
    assert Orchestrator([second]).run(tmp_path, data=prs) == prs
    assert second.seen == ["2", "3"]


def test_events_are_json() -> None:
    samples: list[Event] = [
        StageStarted(stage="mine", total=2),
        StageFinished(stage="mine", skipped=True),
        PRMined(pr_id="1"),
        PRFiltered(pr_id="2", reason="bot"),
        ClassificationAttempt(pr_id="3"),
        EvaluatorVerdict(pr_id="3", approved=False),
        LoopFinished(pr_id="3"),
        SynthesisChunk(text="hello"),
        RateLimited(wait_seconds=15),
        Error(message="boom", stage="classify"),
    ]
    for event in samples:
        payload = json.loads(event.model_dump_json())
        assert payload["type"] == type(event).__name__


def test_sinks_receive_events(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    started = StageStarted(stage="mine", total=1)
    path = tmp_path / "events.jsonl"
    JSONLSink(path).handle(started)
    JSONLSink(path).handle(ClassificationAttempt(pr_id="9"))
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert [line["type"] for line in lines] == ["StageStarted", "ClassificationAttempt"]

    with caplog.at_level(logging.INFO, logger="asgard"):
        LoggingSink().handle(started)
    assert "StageStarted" in caplog.text

    stream = StringIO()
    console = Console(file=stream, force_terminal=True, width=80)
    with CLIProgressSink(console) as sink:
        sink.handle(started)
        task = sink.progress.tasks[0]
        task.start_time = task.get_time() - 90
        sink.handle(ClassificationAttempt(pr_id="9"))
        sink.handle(StageFinished(stage="mine"))
    rendered = stream.getvalue()
    assert "mine" in rendered
    assert "0:01:30" in rendered

    stream = StringIO()
    console = Console(file=stream, force_terminal=True, width=80)
    with CLIProgressSink(console) as sink:
        sink.handle(RateLimited(wait_seconds=15))
    assert "waiting 15s" in stream.getvalue()


def test_run_does_not_write_stdout(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    stream = StringIO()
    sink = CLIProgressSink(Console(file=stream, force_terminal=True, width=80))

    class Echo(Stage):
        name = "input"

        def execute(self, ctx: RunContext, data: Any) -> Any:
            ctx.emit(PRMined(pr_id="1"))
            return {"ok": True}

    Orchestrator(
        [Echo()],
        [sink, JSONLSink(tmp_path / "events.jsonl"), LoggingSink()],
    ).run(tmp_path)
    assert capsys.readouterr().out == ""


def test_core_does_not_print() -> None:
    for path in CORE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            called = isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            assert not (called and node.func.id == "print")
