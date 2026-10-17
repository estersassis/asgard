"""FastAPI app: shell pages, run control, and SSE event stream."""

from __future__ import annotations

import asyncio
import io
import json
import os
import queue
import tomllib
import zipfile
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from asgard.core.config import (
    ConfigError,
    dump_toml,
    list_models,
    load_dotenv,
    load_run,
    probe_run,
)
from asgard.core.pipeline import PIPELINE
from asgard.prompts.synthesizer import SYSTEM_PROMPT
from asgard.ui.session import RunSession, SessionStore

# Allowlisted run-dir files served by the UI (relative to the run output dir).
_RUN_FILE_LOG = "events.jsonl"
_RUN_FILE_CONFIG = "config.json"
_RUN_FILE_PREFILTER_REMOVED = "prefilter/removed.json"
_RUN_FILE_PREFILTER_OUTPUT = "prefilter/output.json"
_RUN_FILE_MINE_OUTPUT = "mine/output.json"
_RUN_FILE_CLASSIFY_TRACE = "classify/trace.jsonl"
_RUN_FILE_CLASSIFY_METRICS = "classify/metrics.json"
_RUN_FILE_CLASSIFY_OUTPUT = "classify/output.json"
_RUN_FILE_CLASSIFY_ITEMS = "classify/items.jsonl"
_RUN_FILE_SECURITY_MD = "SECURITY.md"
_RUN_FILE_CONSOLIDATE_OUTPUT = "consolidate/output.json"
_RUN_FILE_SECURITY_MD_OUTPUT = "security_md/output.json"
_RUN_FILE_SYNTHESIZE_PROMPT = "synthesize/system_prompt.txt"

_UI_ROOT = Path(__file__).resolve().parent
_ENV_PREFILL = (
    "GITHUB_TOKEN",
    "OLLAMA_HOST",
    "GEMINI_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)


class StartRunBody(BaseModel):
    config: Path | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)


class SetupValidateBody(BaseModel):
    overrides: dict[str, Any] = Field(default_factory=dict)
    secrets: dict[str, str] = Field(default_factory=dict)


class SetupModelsBody(BaseModel):
    provider: str
    secrets: dict[str, str] = Field(default_factory=dict)


class SetupTomlBody(BaseModel):
    overrides: dict[str, Any] = Field(default_factory=dict)


class SetupParseBody(BaseModel):
    text: str


class ResumeRunBody(BaseModel):
    from_stage: str = Field(alias="from")
    rerun: bool = False

    model_config = {"populate_by_name": True}


class RegenerateBody(BaseModel):
    from_stage: str = Field(default="synthesize", alias="from")
    prompt: str | None = None

    model_config = {"populate_by_name": True}


class PromptBody(BaseModel):
    text: str


def create_app(
    *,
    opener: Any = None,
    handlers: dict[str, Any] | None = None,
    dotenv: Path | None = Path(".env"),
) -> FastAPI:
    store = SessionStore(opener=opener, handlers=handlers, dotenv=dotenv)
    templates = Jinja2Templates(directory=str(_UI_ROOT / "templates"))
    app = FastAPI(title="ASGARD")
    app.state.store = store
    app.mount("/static", StaticFiles(directory=str(_UI_ROOT / "static")), name="static")

    def _theme(request: Request) -> str:
        return "light" if request.query_params.get("theme") == "light" else "dark"

    def _page(request: Request, screen: str, **extra: Any) -> dict[str, Any]:
        run = request.query_params.get("run")
        context: dict[str, Any] = {
            "screen": screen,
            "theme": _theme(request),
            "run": run if isinstance(run, str) and run else None,
        }
        context.update(extra)
        return context

    def _screen(request: Request, name: str, numeral: str, label: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "screen.html",
            _page(request, name, numeral=numeral, label=label),
        )

    def _session_or_404(run_id: str) -> RunSession:
        session = store.get(run_id)
        if session is None:
            raise HTTPException(status_code=404, detail="run not found")
        return session

    def _run_file(
        session: RunSession,
        relative: str,
        media_type: str,
        *,
        filename: str | None = None,
    ) -> FileResponse:
        if session.output_dir is None:
            raise HTTPException(status_code=404, detail="file not found")
        root = session.output_dir.resolve()
        path = (root / relative).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(status_code=404, detail="file not found")
        return FileResponse(path, media_type=media_type, filename=filename)

    def _prompt_path(session: RunSession) -> Path:
        if session.output_dir is None:
            raise HTTPException(status_code=404, detail="file not found")
        root = session.output_dir.resolve()
        path = (root / _RUN_FILE_SYNTHESIZE_PROMPT).resolve()
        if not path.is_relative_to(root):
            raise HTTPException(status_code=404, detail="file not found")
        return path

    @app.get("/", response_class=HTMLResponse)
    @app.get("/setup", response_class=HTMLResponse, name="setup")
    async def setup(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "setup.html",
            _page(request, "setup"),
        )

    @app.get("/pipeline", response_class=HTMLResponse, name="pipeline")
    async def pipeline(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "pipeline.html",
            _page(request, "pipeline"),
        )

    @app.get("/council", response_class=HTMLResponse, name="council")
    async def council(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "council.html",
            _page(request, "council"),
        )

    @app.get("/guideline", response_class=HTMLResponse, name="guideline")
    async def guideline(request: Request) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "guideline.html",
            _page(request, "guideline"),
        )

    @app.get("/forge", response_class=HTMLResponse, name="forge")
    async def forge(request: Request) -> HTMLResponse:
        return _screen(request, "forge", "V", "Forge")

    @app.get("/api/setup/env")
    async def setup_env() -> dict[str, str]:
        if store.dotenv is not None:
            load_dotenv(store.dotenv)
        values: dict[str, str] = {}
        for key in _ENV_PREFILL:
            raw = os.environ.get(key)
            if raw:
                values[key] = raw
        values.setdefault("OLLAMA_HOST", "http://localhost:11434")
        return values

    @app.post("/api/setup/validate")
    async def setup_validate(body: SetupValidateBody) -> dict[str, Any]:
        try:
            config, credentials = load_run(
                overrides=body.overrides or None,
                secrets=body.secrets or None,
            )
            return probe_run(config, credentials, opener=store.opener)
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/setup/models")
    async def setup_models(body: SetupModelsBody) -> dict[str, Any]:
        if store.dotenv is not None:
            load_dotenv(store.dotenv)
        try:
            models = list_models(
                body.provider,
                secrets=body.secrets or None,
                opener=store.opener,
            )
        except ConfigError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"provider": body.provider, "models": models}

    @app.post("/api/setup/toml")
    async def setup_toml(body: SetupTomlBody) -> PlainTextResponse:
        data = dict(body.overrides or {})
        leaked = [key for key in ("github_token", "keys") if key in data]
        text = dump_toml(data)
        if leaked or "github_token" in text or "[keys]" in text:
            raise HTTPException(status_code=400, detail="secrets must not be saved to asgard.toml")
        return PlainTextResponse(text, media_type="text/toml")

    @app.post("/api/setup/parse")
    async def setup_parse(body: SetupParseBody) -> dict[str, Any]:
        raw = body.text.strip()
        if not raw:
            raise HTTPException(status_code=400, detail="empty settings")
        try:
            if raw.startswith("{"):
                data = json.loads(raw)
            else:
                data = tomllib.loads(raw)
        except (json.JSONDecodeError, tomllib.TOMLDecodeError) as exc:
            raise HTTPException(status_code=400, detail=f"invalid settings: {exc}") from exc
        if not isinstance(data, dict):
            raise HTTPException(status_code=400, detail="settings must be a table")
        data.pop("github_token", None)
        data.pop("keys", None)
        return data

    @app.post("/api/runs")
    async def start_run(body: StartRunBody) -> dict[str, str]:
        session = store.create()
        try:
            output_dir = session.start(
                config_path=body.config,
                overrides=body.overrides or None,
                secrets=body.secrets or None,
            )
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": session.id, "output_dir": str(output_dir)}

    @app.post("/api/runs/{run_id}/stop")
    async def stop_run(run_id: str) -> dict[str, str]:
        session = store.get(run_id)
        if session is None:
            raise HTTPException(status_code=404, detail="run not found")
        session.stop()
        return {"id": run_id, "status": "stopping"}

    @app.post("/api/runs/{run_id}/resume")
    async def resume_run(run_id: str, body: ResumeRunBody) -> dict[str, str]:
        session = store.get(run_id)
        if session is None:
            raise HTTPException(status_code=404, detail="run not found")
        if body.from_stage not in PIPELINE:
            raise HTTPException(status_code=400, detail=f"unknown stage: {body.from_stage}")
        try:
            session.join(timeout=30)
            output_dir = session.resume(body.from_stage, rerun=body.rerun)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": run_id, "output_dir": str(output_dir)}

    @app.post("/api/runs/{run_id}/regenerate")
    async def regenerate_run(run_id: str, body: RegenerateBody) -> dict[str, str]:
        session = store.get(run_id)
        if session is None:
            raise HTTPException(status_code=404, detail="run not found")
        if body.from_stage not in PIPELINE:
            raise HTTPException(status_code=400, detail=f"unknown stage: {body.from_stage}")
        try:
            session.join(timeout=30)
            output_dir = session.regenerate_from(body.from_stage, prompt=body.prompt)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return {"id": run_id, "output_dir": str(output_dir)}

    @app.get("/api/runs/{run_id}/events")
    async def run_events(run_id: str) -> EventSourceResponse:
        session = store.get(run_id)
        if session is None:
            raise HTTPException(status_code=404, detail="run not found")

        async def stream() -> Any:
            while True:
                try:
                    event = await asyncio.to_thread(session.sink.get, 0.5)
                except queue.Empty:
                    if not session.running:
                        # Drain anything left after the worker finished.
                        for leftover in session.sink.drain():
                            yield {
                                "event": leftover.get("type", "message"),
                                "data": json.dumps(leftover),
                            }
                        break
                    yield {"event": "ping", "data": ""}
                    continue
                if event is None:
                    break
                yield {"event": event.get("type", "message"), "data": json.dumps(event)}

        return EventSourceResponse(stream())

    @app.get("/api/runs/{run_id}")
    async def run_status(run_id: str) -> dict[str, Any]:
        session = _session_or_404(run_id)
        return {
            "id": session.id,
            "running": session.running,
            "output_dir": str(session.output_dir) if session.output_dir else None,
            "error": session.error,
        }

    @app.get("/api/runs/{run_id}/log")
    async def run_log(run_id: str) -> FileResponse:
        return _run_file(_session_or_404(run_id), _RUN_FILE_LOG, "text/plain")

    @app.get("/api/runs/{run_id}/config")
    async def run_config(run_id: str) -> FileResponse:
        return _run_file(_session_or_404(run_id), _RUN_FILE_CONFIG, "application/json")

    @app.get("/api/runs/{run_id}/prefilter/removed")
    async def run_prefilter_removed(run_id: str) -> FileResponse:
        return _run_file(
            _session_or_404(run_id),
            _RUN_FILE_PREFILTER_REMOVED,
            "application/json",
        )

    @app.get("/api/runs/{run_id}/prefilter/output")
    async def run_prefilter_output(run_id: str) -> FileResponse:
        return _run_file(
            _session_or_404(run_id),
            _RUN_FILE_PREFILTER_OUTPUT,
            "application/json",
        )

    @app.get("/api/runs/{run_id}/mine/output")
    async def run_mine_output(run_id: str) -> FileResponse:
        return _run_file(
            _session_or_404(run_id),
            _RUN_FILE_MINE_OUTPUT,
            "application/json",
        )

    @app.get("/api/runs/{run_id}/mine/prs/{number}")
    async def run_mine_pr(run_id: str, number: str) -> FileResponse:
        if not number.isdigit():
            raise HTTPException(status_code=404, detail="file not found")
        return _run_file(
            _session_or_404(run_id),
            f"mine/prs/{number}.json",
            "application/json",
        )

    @app.get("/api/runs/{run_id}/classify/trace")
    async def run_classify_trace(run_id: str) -> FileResponse:
        return _run_file(
            _session_or_404(run_id),
            _RUN_FILE_CLASSIFY_TRACE,
            "text/plain",
        )

    @app.get("/api/runs/{run_id}/classify/metrics")
    async def run_classify_metrics(run_id: str) -> FileResponse:
        return _run_file(
            _session_or_404(run_id),
            _RUN_FILE_CLASSIFY_METRICS,
            "application/json",
        )

    @app.get("/api/runs/{run_id}/classify/output")
    async def run_classify_output(run_id: str) -> FileResponse:
        return _run_file(
            _session_or_404(run_id),
            _RUN_FILE_CLASSIFY_OUTPUT,
            "application/json",
        )

    @app.get("/api/runs/{run_id}/classify/items")
    async def run_classify_items(run_id: str) -> FileResponse:
        return _run_file(
            _session_or_404(run_id),
            _RUN_FILE_CLASSIFY_ITEMS,
            "text/plain",
        )

    @app.get("/api/runs/{run_id}/SECURITY.md")
    async def run_security_md(run_id: str) -> FileResponse:
        return _run_file(
            _session_or_404(run_id),
            _RUN_FILE_SECURITY_MD,
            "text/markdown",
            filename="SECURITY.md",
        )

    @app.get("/api/runs/{run_id}/consolidate/output")
    async def run_consolidate_output(run_id: str) -> FileResponse:
        return _run_file(
            _session_or_404(run_id),
            _RUN_FILE_CONSOLIDATE_OUTPUT,
            "application/json",
        )

    @app.get("/api/runs/{run_id}/security_md/output")
    async def run_security_md_output(run_id: str) -> FileResponse:
        return _run_file(
            _session_or_404(run_id),
            _RUN_FILE_SECURITY_MD_OUTPUT,
            "application/json",
        )

    @app.get("/api/runs/{run_id}/synthesize/prompt")
    async def run_synthesize_prompt(run_id: str) -> PlainTextResponse:
        session = _session_or_404(run_id)
        path = _prompt_path(session)
        text = path.read_text(encoding="utf-8") if path.is_file() else SYSTEM_PROMPT
        return PlainTextResponse(text, media_type="text/plain; charset=utf-8")

    @app.put("/api/runs/{run_id}/synthesize/prompt")
    async def put_synthesize_prompt(run_id: str, body: PromptBody) -> dict[str, str]:
        session = _session_or_404(run_id)
        path = _prompt_path(session)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body.text, encoding="utf-8")
        return {"path": _RUN_FILE_SYNTHESIZE_PROMPT}

    @app.get("/api/runs/{run_id}/bundle.zip")
    async def run_bundle_zip(run_id: str) -> Response:
        session = _session_or_404(run_id)
        if session.output_dir is None:
            raise HTTPException(status_code=404, detail="file not found")
        root = session.output_dir.resolve()
        if not root.is_dir():
            raise HTTPException(status_code=404, detail="file not found")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(root.rglob("*")):
                if not path.is_file():
                    continue
                relative = path.resolve().relative_to(root)
                archive.write(path, relative.as_posix())
        return Response(
            content=buffer.getvalue(),
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="bundle.zip"'},
        )

    return app


def serve(*, host: str = "127.0.0.1", port: int = 8000) -> None:
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit(
            "the web UI requires asgard[ui]; install with: pip install asgard[ui]"
        ) from exc
    uvicorn.run(create_app(), host=host, port=port)
