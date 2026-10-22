"""One live run controlled through the core pipeline API."""

from __future__ import annotations

import json
import threading
import uuid
from pathlib import Path
from typing import Any

from asgard.core.config import prepare_run
from asgard.core.pipeline import (
    PIPELINE,
    classify_retryable_ids,
    clear_from,
    drop_classify_items,
    run_pipeline,
)
from asgard.core.stage import CancelledError
from asgard.ui.runs import discover_runs, resolve_run_dir, stable_run_id
from asgard.ui.sink import UISink

_MAP_PROMPT = "map_prompt.txt"
_REDUCE_PROMPT = "reduce_prompt.txt"
_LEGACY_PROMPT = "system_prompt.txt"


def _write_map_batch(output_dir: Path, mode: str, size: int | None) -> None:
    """Persist the map strategy on the run. ``full`` clears the batch size."""
    path = output_dir / "config.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("run configuration is invalid")
    data["synthesize_batch_size"] = size if mode == "batch" else None
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


class RunSession:
    def __init__(
        self,
        *,
        opener: Any = None,
        handlers: dict[str, Any] | None = None,
        dotenv: Path | None = Path(".env"),
        session_id: str | None = None,
    ) -> None:
        self.id = session_id or uuid.uuid4().hex
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

    @classmethod
    def open_existing(
        cls,
        output_dir: Path,
        *,
        session_id: str,
        opener: Any = None,
        handlers: dict[str, Any] | None = None,
        dotenv: Path | None = Path(".env"),
    ) -> RunSession:
        """Attach to a finished or interrupted run directory without starting work."""
        session = cls(
            opener=opener,
            handlers=handlers,
            dotenv=dotenv,
            session_id=session_id,
        )
        session.output_dir = output_dir
        session.sink.close()
        return session

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

    def resume(self, from_stage: str, *, rerun: bool = False) -> Path:
        if from_stage not in PIPELINE:
            raise ValueError(f"unknown stage: {from_stage}")
        if self.output_dir is None:
            raise RuntimeError("no run to resume")
        self.join()
        if rerun:
            clear_from(self.output_dir, from_stage)
        return self.start(
            config_path=self.output_dir / "config.json",
            start=from_stage,
        )

    def regenerate_from(
        self,
        stage: str,
        *,
        prompt: str | None = None,
        map_prompt: str | None = None,
        reduce_prompt: str | None = None,
        map_mode: str | None = None,
        map_batch_size: int | None = None,
    ) -> Path:
        """Clear ``stage`` and later outputs, then resume from that stage.

        ``prompt`` is a legacy alias for ``reduce_prompt``.
        """
        if stage not in PIPELINE:
            raise ValueError(f"unknown stage: {stage}")
        if self.output_dir is None:
            raise RuntimeError("no run to resume")
        self.join()
        if reduce_prompt is None:
            reduce_prompt = prompt
        synthesize = self.output_dir / "synthesize"
        if map_prompt is not None or reduce_prompt is not None:
            synthesize.mkdir(parents=True, exist_ok=True)
        if map_prompt is not None:
            (synthesize / _MAP_PROMPT).write_text(map_prompt, encoding="utf-8")
        if reduce_prompt is not None:
            (synthesize / _REDUCE_PROMPT).write_text(reduce_prompt, encoding="utf-8")
            legacy = synthesize / _LEGACY_PROMPT
            if legacy.is_file():
                legacy.unlink()
        if map_mode == "batch" and (map_batch_size is None or map_batch_size < 1):
            raise ValueError("map_batch_size is required")
        if map_mode in ("full", "batch"):
            _write_map_batch(self.output_dir, map_mode, map_batch_size)
        clear_from(self.output_dir, stage)
        # clear_from keeps map.json for resume-after-reduce-failure; regenerate
        # must re-run map so edited map prompts take effect.
        map_path = self.output_dir / "synthesize" / "map.json"
        if map_path.is_file():
            map_path.unlink()
        return self.start(
            config_path=self.output_dir / "config.json",
            start=stage,
        )

    def retry_classify(
        self,
        pr_ids: list[str] | None = None,
        *,
        all_errors: bool = False,
    ) -> tuple[Path, list[str]]:
        """Drop error/unresolved classify checkpoints and resume classify for those PRs.

        ``all_errors`` retries every retryable checkpoint (``error`` and
        ``unresolved_*``), not only hard errors.
        """
        if self.output_dir is None:
            raise RuntimeError("no run to resume")
        if self.running:
            raise RuntimeError("a run is already in progress")
        self.join()
        retryable = set(classify_retryable_ids(self.output_dir))
        if all_errors:
            targets = retryable
        else:
            requested = {item_id for item_id in (pr_ids or []) if item_id}
            targets = requested & retryable
            if requested and not targets:
                raise ValueError(
                    "none of the selected PRs are error or unresolved checkpoints"
                    + (
                        f" ({len(retryable)} still on disk)"
                        if retryable
                        else ""
                    )
                )
        if not targets:
            raise ValueError("no error or unresolved pull requests to retry")
        removed = drop_classify_items(self.output_dir, targets)
        if not removed:
            raise ValueError("no error or unresolved pull requests to retry")
        output_dir = self.start(
            config_path=self.output_dir / "config.json",
            start="classify",
        )
        return output_dir, removed

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

    def rekey(self, session: RunSession, new_id: str) -> None:
        """Point the session at a stable on-disk id when the run directory is known."""
        if not new_id or new_id == session.id:
            return
        with self._lock:
            self._sessions.pop(session.id, None)
            session.id = new_id
            self._sessions[new_id] = session

    def bind_stable_id(self, session: RunSession) -> str:
        """Prefer ``repo--stamp`` when the output dir lives under runs/."""
        if session.output_dir is None:
            return session.id
        stable = stable_run_id(session.output_dir)
        if stable is None:
            return session.id
        self.rekey(session, stable)
        return session.id

    def get(self, session_id: str) -> RunSession | None:
        with self._lock:
            found = self._sessions.get(session_id)
            if found is not None:
                return found
        path = resolve_run_dir(session_id)
        if path is None:
            return None
        session = RunSession.open_existing(
            path,
            session_id=session_id,
            opener=self.opener,
            handlers=self.handlers,
            dotenv=self.dotenv,
        )
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is not None:
                return existing
            self._sessions[session_id] = session
            return session

    def list_runs(self) -> list[dict[str, Any]]:
        disk = discover_runs()
        with self._lock:
            live = {
                session.id: session
                for session in self._sessions.values()
                if session.output_dir is not None
            }
        for item in disk:
            session = live.get(item["id"])
            item["running"] = bool(session and session.running)
            item["error"] = session.error if session else None
        # Include in-memory sessions whose dirs are outside the scanned roots.
        known = {item["id"] for item in disk}
        for session_id, session in live.items():
            if session_id in known or session.output_dir is None:
                continue
            disk.append(
                {
                    "id": session_id,
                    "output_dir": str(session.output_dir),
                    "repo": _repo_from_dir(session.output_dir),
                    "stamp": session.output_dir.name,
                    "complete": (session.output_dir / "SECURITY.md").is_file(),
                    "running": session.running,
                    "error": session.error,
                }
            )
        return disk


def _repo_from_dir(output_dir: Path) -> str:
    config = output_dir / "config.json"
    if not config.is_file():
        return output_dir.name
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return output_dir.name
    if isinstance(data, dict) and isinstance(data.get("repo"), str) and data["repo"]:
        return data["repo"]
    return output_dir.name
