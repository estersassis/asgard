"""Background pilot runs for the Forge UI. Transport only — gates live in core."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from asgard.core.config import ConfigError, load_dotenv, load_run
from asgard.core.pilot.gate import GateThresholds, PromoteError, promote
from asgard.core.pilot.runner import PilotMode, PilotProgress, run_pilot
from asgard.core.pilot.set import BUNDLED_SETS, PilotSet, load_pilot_set
from asgard.providers.factory import LLMFactory

PilotStatus = Literal["running", "done", "error"]

_DEFAULT_PILOT_ROOT = Path("runs") / "_pilot"
_UPLOADS_DIR = _DEFAULT_PILOT_ROOT / "uploads"
_METRICS = "metrics.json"
_ERROR = "error.txt"


class PilotSession:
    def __init__(
        self,
        *,
        handlers: dict[str, Any] | None = None,
        dotenv: Path | None = Path(".env"),
        root: Path | None = None,
        session_id: str | None = None,
    ) -> None:
        self.root = root if root is not None else _DEFAULT_PILOT_ROOT
        self.id = session_id or _new_run_id(self.root)
        self.handlers = handlers
        self.dotenv = dotenv
        self.output_dir: Path | None = None
        self.error: str | None = None
        self.status: PilotStatus = "running"
        self.progress: dict[str, Any] | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._running = False

    @classmethod
    def open_existing(
        cls,
        output_dir: Path,
        *,
        session_id: str,
        handlers: dict[str, Any] | None = None,
        dotenv: Path | None = Path(".env"),
        root: Path | None = None,
    ) -> PilotSession:
        session = cls(
            handlers=handlers,
            dotenv=dotenv,
            root=root,
            session_id=session_id,
        )
        session.output_dir = output_dir
        error_path = output_dir / _ERROR
        metrics_path = output_dir / _METRICS
        if metrics_path.is_file():
            session.status = "done"
            session.error = None
        elif error_path.is_file():
            session.status = "error"
            session.error = error_path.read_text(encoding="utf-8").strip() or "pilot failed"
        else:
            session.status = "error"
            session.error = "pilot run incomplete"
        return session

    @property
    def running(self) -> bool:
        return self._running

    def start(
        self,
        *,
        candidate: str,
        active: str | None = None,
        pilot_set: str | Path = "binary_v2",
        config: Path | None = None,
        thresholds: GateThresholds | None = None,
        registry_dir: Path | None = None,
        output_dir: Path | None = None,
        mode: PilotMode = "classifier",
    ) -> Path:
        with self._lock:
            if self._running:
                raise RuntimeError("a pilot run is already in progress")
            if self.dotenv is not None:
                load_dotenv(self.dotenv)
            try:
                run_config, credentials = load_run(config)
            except ConfigError:
                raise
            active_id = (active or run_config.prompts.classifier).strip() or "default"
            root = registry_dir if registry_dir is not None else run_config.prompts.registry_dir
            factory = LLMFactory(run_config.llm, credentials, handlers=self.handlers or {})
            out = Path(output_dir) if output_dir is not None else self.root / self.id
            out.mkdir(parents=True, exist_ok=True)
            self.id = out.name
            self.output_dir = out
            self.error = None
            self.status = "running"
            self.progress = {
                "phase": "active",
                "phase_label": "Active prompt",
                "done": 0,
                "total": 0,
                "overall_done": 0,
                "overall_total": 0,
                "message": "Running pilot…",
            }
            self._running = True
            self._thread = threading.Thread(
                target=self._run,
                kwargs={
                    "pilot_set": pilot_set,
                    "candidate": candidate,
                    "active": active_id,
                    "factory": factory,
                    "registry_dir": root,
                    "max_iterations": run_config.max_iterations,
                    "thresholds": thresholds,
                    "output_dir": out,
                    "mode": mode,
                },
                daemon=True,
                name=f"asgard-pilot-{self.id[:8]}",
            )
            self._thread.start()
            return out

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)

    def _set_progress(self, progress: PilotProgress) -> None:
        with self._lock:
            self.progress = progress.model_dump(mode="json")

    def progress_snapshot(self) -> dict[str, Any] | None:
        with self._lock:
            if self.progress is None:
                return None
            return dict(self.progress)

    def _run(
        self,
        *,
        pilot_set: str | Path,
        candidate: str,
        active: str,
        factory: LLMFactory,
        registry_dir: Path | None,
        max_iterations: int,
        thresholds: GateThresholds | None,
        output_dir: Path,
        mode: PilotMode = "classifier",
    ) -> None:
        try:
            run_pilot(
                pilot_set,
                candidate=candidate,
                active=active,
                factory=factory,
                registry_dir=registry_dir,
                max_iterations=max_iterations,
                thresholds=thresholds,
                output_dir=output_dir,
                mode=mode,
                on_progress=self._set_progress,
            )
            self.status = "done"
            self.error = None
            error_path = output_dir / _ERROR
            if error_path.is_file():
                error_path.unlink()
        except Exception as exc:
            self.status = "error"
            self.error = str(exc)
            try:
                (output_dir / _ERROR).write_text(self.error + "\n", encoding="utf-8")
            except OSError:
                pass
        finally:
            with self._lock:
                self._running = False


class PilotStore:
    def __init__(
        self,
        *,
        handlers: dict[str, Any] | None = None,
        dotenv: Path | None = Path(".env"),
        root: Path | None = None,
    ) -> None:
        self.handlers = handlers
        self.dotenv = dotenv
        self.root = root if root is not None else _DEFAULT_PILOT_ROOT
        self.uploads_dir = self.root / "uploads"
        self._sessions: dict[str, PilotSession] = {}
        self._lock = threading.Lock()

    def create(self) -> PilotSession:
        session = PilotSession(
            handlers=self.handlers,
            dotenv=self.dotenv,
            root=self.root,
            session_id=_new_run_id(self.root),
        )
        with self._lock:
            self._sessions[session.id] = session
        return session

    def rekey(self, session: PilotSession, new_id: str) -> None:
        if not new_id or new_id == session.id:
            return
        with self._lock:
            self._sessions.pop(session.id, None)
            session.id = new_id
            self._sessions[new_id] = session

    def get(self, run_id: str) -> PilotSession | None:
        with self._lock:
            found = self._sessions.get(run_id)
            if found is not None:
                return found
        path = _resolve_pilot_dir(self.root, run_id)
        if path is None:
            return None
        session = PilotSession.open_existing(
            path,
            session_id=run_id,
            handlers=self.handlers,
            dotenv=self.dotenv,
            root=self.root,
        )
        with self._lock:
            existing = self._sessions.get(run_id)
            if existing is not None:
                return existing
            self._sessions[run_id] = session
            return session

    def list_sets(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        for set_id in sorted(BUNDLED_SETS):
            try:
                pilot = load_pilot_set(set_id)
            except (OSError, ValueError, LookupError, json.JSONDecodeError, ValidationError):
                continue
            # Selectable id is the bundled key (filename stem), not pilot.id,
            # so resolve_pilot_set / load_pilot_set always round-trip.
            summary = _set_summary(pilot, bundled=True)
            summary["id"] = set_id
            items.append(summary)
            seen.add(set_id)
            seen.add(pilot.id)
        if self.uploads_dir.is_dir():
            for path in sorted(self.uploads_dir.glob("*.json")):
                try:
                    pilot = load_pilot_set(path)
                except (OSError, ValueError, LookupError, json.JSONDecodeError, ValidationError):
                    continue
                if pilot.id in seen:
                    continue
                summary = _set_summary(pilot, bundled=False)
                summary["path"] = str(path)
                items.append(summary)
                seen.add(pilot.id)
        return items

    def save_set(self, pilot: PilotSet) -> dict[str, str]:
        set_id = pilot.id.strip()
        _check_set_id(set_id)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)
        path = (self.uploads_dir / f"{set_id}.json").resolve()
        root = self.uploads_dir.resolve()
        if not path.is_relative_to(root):
            raise ValueError("invalid pilot set id")
        path.write_text(
            json.dumps(pilot.model_dump(mode="json"), indent=2) + "\n",
            encoding="utf-8",
        )
        return {"id": set_id, "path": str(path)}

    def resolve_pilot_set(self, source: str) -> str | Path:
        text = source.strip()
        if not text:
            raise ValueError("pilot set source must not be empty")
        if text in BUNDLED_SETS:
            return text
        upload = (self.uploads_dir / f"{text}.json").resolve()
        if (
            self.uploads_dir.is_dir()
            and upload.is_file()
            and upload.is_relative_to(self.uploads_dir.resolve())
        ):
            return upload
        path = Path(text)
        if path.is_file():
            return path
        raise LookupError(
            f"unknown pilot set: {text!r} "
            f"(known bundled: {', '.join(sorted(BUNDLED_SETS))})"
        )

    def metrics_path(self, run_id: str) -> Path | None:
        session = self.get(run_id)
        if session is None or session.output_dir is None:
            return None
        path = (session.output_dir / _METRICS).resolve()
        root = session.output_dir.resolve()
        if not path.is_relative_to(root) or not path.is_file():
            return None
        return path


def promote_from_api(
    *,
    candidate: str,
    config: Path,
    from_result: Path | None = None,
    role: str = "classifier",
    root: Path | None = None,
) -> dict[str, Any]:
    """Thin wrapper over :func:`promote` with CLI-style evidence defaults."""
    evidence = from_result
    pilot_root = root if root is not None else _DEFAULT_PILOT_ROOT
    if evidence is None:
        evidence = _latest_pilot_result(pilot_root)
        if evidence is None:
            raise PromoteError(
                "no pilot result found under runs/_pilot/; pass from_result"
            )
    _require_matching_candidate(evidence, candidate)
    result = promote(
        config,
        version_id=candidate,
        role=role,
        from_result=evidence,
    )
    return result.model_dump(mode="json")


def _new_run_id(root: Path | None = None) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = root if root is not None else _DEFAULT_PILOT_ROOT
    candidate = stamp
    suffix = 0
    while (base / candidate).exists():
        suffix += 1
        candidate = f"{stamp}-{suffix}"
    return candidate


def _resolve_pilot_dir(root: Path, run_id: str) -> Path | None:
    if not run_id or "/" in run_id or "\\" in run_id or run_id in (".", ".."):
        return None
    path = (root / run_id).resolve()
    try:
        if not path.is_relative_to(root.resolve()):
            return None
    except OSError:
        return None
    if not path.is_dir():
        return None
    return path


def _set_summary(pilot: PilotSet, *, bundled: bool) -> dict[str, Any]:
    return {
        "id": pilot.id,
        "name": pilot.name,
        "bundled": bundled,
        "item_count": len(pilot.items),
    }


def _check_set_id(set_id: str) -> None:
    if not set_id or "/" in set_id or "\\" in set_id or set_id in (".", ".."):
        raise ValueError("invalid pilot set id")
    if set_id.endswith(".json"):
        raise ValueError("pilot set id must not include .json")


def _latest_pilot_result(root: Path) -> Path | None:
    if not root.is_dir():
        return None
    metrics = [
        path
        for path in root.glob(f"*/{_METRICS}")
        if path.is_file() and path.parent.name != "uploads"
    ]
    if not metrics:
        return None
    return max(metrics, key=lambda path: path.stat().st_mtime)


def _require_matching_candidate(metrics_path: Path, candidate: str) -> None:
    try:
        data = metrics_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PromoteError(f"cannot read pilot result: {metrics_path}") from exc
    try:
        payload = json.loads(data)
    except json.JSONDecodeError as exc:
        raise PromoteError(f"invalid pilot result JSON: {exc}") from None
    if not isinstance(payload, dict):
        return
    recorded = payload.get("candidate_prompt")
    if isinstance(recorded, str) and recorded.strip() and recorded.strip() != candidate.strip():
        raise PromoteError(
            f"pilot result candidate {recorded!r} does not match candidate {candidate!r}"
        )
