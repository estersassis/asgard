"""One live run controlled through the core pipeline API."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any

from asgard.core.config import prepare_run
from asgard.core.pipeline import PIPELINE, run_pipeline
from asgard.core.stage import CancelledError
from asgard.ui.sink import UISink


class RunSession:
    def __init__(
        self,
        *,
        opener: Any = None,
        handlers: dict[str, Any] | None = None,
        dotenv: Path | None = Path(".env"),
    ) -> None:
        self.id = uuid.uuid4().hex
        self.opener = opener
        self.handlers = handlers
        self.dotenv = dotenv
        self.sink = UISink()
        self.output_dir: Path | None = None
        self.error: str | None = None
        self._cancel = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    def start(
        self,
        *,
        config_path: Path | None = None,
        overrides: dict[str, Any] | None = None,
        secrets: dict[str, str] | None = None,
        start: str | None = None,
    ) -> Path:
        with self._lock:
            if self._running:
                raise RuntimeError("a run is already in progress")
            self._cancel.clear()
            self.error = None
            self.sink = UISink()
            config = prepare_run(
                overrides,
                config_path,
                dotenv=self.dotenv,
                opener=self.opener,
                secrets=secrets,
            )
            self.output_dir = config.output_dir
            self._running = True
            self._thread = threading.Thread(
                target=self._run,
                kwargs={"config": config, "start": start},
                daemon=True,
                name=f"asgard-run-{self.id[:8]}",
            )
            self._thread.start()
            return config.output_dir

    def stop(self) -> None:
        self._cancel.set()

    def resume(self, from_stage: str) -> Path:
        if from_stage not in PIPELINE:
            raise ValueError(f"unknown stage: {from_stage}")
        if self.output_dir is None:
            raise RuntimeError("no run to resume")
        self.join()
        return self.start(
            config_path=self.output_dir / "config.json",
            start=from_stage,
        )

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def _run(self, *, config: Any, start: str | None) -> None:
        try:
            run_pipeline(
                config,
                start=start,
                sinks=[self.sink],
                opener=self.opener,
                handlers=self.handlers,
                cancel=self._cancel.is_set,
            )
        except CancelledError as exc:
            self.error = str(exc)
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.sink.close()
            with self._lock:
                self._running = False


class SessionStore:
    def __init__(
        self,
        *,
        opener: Any = None,
        handlers: dict[str, Any] | None = None,
        dotenv: Path | None = Path(".env"),
    ) -> None:
        self.opener = opener
        self.handlers = handlers
        self.dotenv = dotenv
        self._sessions: dict[str, RunSession] = {}
        self._lock = threading.Lock()

    def create(self) -> RunSession:
        session = RunSession(
            opener=self.opener,
            handlers=self.handlers,
            dotenv=self.dotenv,
        )
        with self._lock:
            self._sessions[session.id] = session
        return session

    def get(self, session_id: str) -> RunSession | None:
        with self._lock:
            return self._sessions.get(session_id)
