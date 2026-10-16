"""FastAPI app: shell pages, run control, and SSE event stream."""

from __future__ import annotations

import asyncio
import json
import queue
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

from asgard.core.pipeline import PIPELINE
from asgard.ui.session import SessionStore

_UI_ROOT = Path(__file__).resolve().parent


class StartRunBody(BaseModel):
    config: Path | None = None
    overrides: dict[str, Any] = Field(default_factory=dict)


class ResumeRunBody(BaseModel):
    from_stage: str = Field(alias="from")

    model_config = {"populate_by_name": True}


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

    def _screen(request: Request, name: str, numeral: str, label: str) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "screen.html",
            {
                "screen": name,
                "numeral": numeral,
                "label": label,
                "theme": _theme(request),
            },
        )

    @app.get("/", response_class=HTMLResponse)
    @app.get("/setup", response_class=HTMLResponse, name="setup")
    async def setup(request: Request) -> HTMLResponse:
        return _screen(request, "setup", "I", "Setup")

    @app.get("/pipeline", response_class=HTMLResponse, name="pipeline")
    async def pipeline(request: Request) -> HTMLResponse:
        return _screen(request, "pipeline", "II", "Pipeline")

    @app.get("/council", response_class=HTMLResponse, name="council")
    async def council(request: Request) -> HTMLResponse:
        return _screen(request, "council", "III", "Council")

    @app.get("/guideline", response_class=HTMLResponse, name="guideline")
    async def guideline(request: Request) -> HTMLResponse:
        return _screen(request, "guideline", "IV", "Guideline")

    @app.get("/forge", response_class=HTMLResponse, name="forge")
    async def forge(request: Request) -> HTMLResponse:
        return _screen(request, "forge", "V", "Forge")

    @app.post("/api/runs")
    async def start_run(body: StartRunBody) -> dict[str, str]:
        session = store.create()
        try:
            output_dir = session.start(
                config_path=body.config,
                overrides=body.overrides or None,
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
            output_dir = session.resume(body.from_stage)
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
        session = store.get(run_id)
        if session is None:
            raise HTTPException(status_code=404, detail="run not found")
        return {
            "id": session.id,
            "running": session.running,
            "output_dir": str(session.output_dir) if session.output_dir else None,
            "error": session.error,
        }

    return app


def serve(*, host: str = "127.0.0.1", port: int = 8000) -> None:
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit(
            "the web UI requires asgard[ui]; install with: pip install asgard[ui]"
        ) from exc
    uvicorn.run(create_app(), host=host, port=port)
