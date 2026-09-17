from __future__ import annotations

import hmac
import json
from dataclasses import asdict
from pathlib import Path

from fastapi import (
    APIRouter,
    Cookie,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
)
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from . import planning, sessions
from .config import Settings
from .engine import Engine
from .export import render_csv
from .models import JobSpec, ReplaySpec

VERSION = "0.5.0"
WEB_DIR = Path(__file__).parent / "web"
_ASSETS = {"app.css": "text/css", "app.js": "text/javascript; charset=utf-8"}


class LoginRequest(BaseModel):
    token: str


class InvestigationPlanRequest(BaseModel):
    value: str
    kind: str | None = None


class DatasetPlanRequest(BaseModel):
    description: str
    fields: list[str] | None = None
    seeds: list[str] | None = None


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    if len(settings.api_token) < 24 or settings.api_token.startswith("replace-this"):
        raise RuntimeError("HARVEST_API_TOKEN must be set to at least 24 characters")
    engine = Engine(settings)

    def authenticate(
        authorization: str | None = Header(default=None),
        harvest_session: str | None = Cookie(default=None),
    ):
        expected = "Bearer " + settings.api_token
        if authorization and hmac.compare_digest(authorization.encode(), expected.encode()):
            return
        # A same-site HttpOnly capability cookie, never the raw token itself, lets the
        # local browser UI authenticate without ever putting the operator token in
        # rendered HTML, JS, or logs. See `sessions.py`.
        if sessions.verify(settings.api_token, harvest_session):
            return
        raise HTTPException(status_code=401, detail="invalid bearer token")

    app = FastAPI(
        title="Harvest Platform",
        version=VERSION,
        description="Durable jobs, evidence and source observations. Run a separate harvest worker.",
    )

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'"
        )
        return response

    @app.exception_handler(KeyError)
    async def not_found(request, exc):
        return Response(
            content='{"detail":"not found"}', media_type="application/json", status_code=404
        )

    # ---- Public: the static browser shell and session login. No job/evidence data here;
    # every route that touches job or evidence data lives on `protected` below. ----

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def index():
        return (WEB_DIR / "index.html").read_text()

    @app.get("/assets/{name}", include_in_schema=False)
    def assets(name: str):
        media = _ASSETS.get(name)
        if media is None:
            raise HTTPException(status_code=404, detail="not found")
        return Response(
            (WEB_DIR / name).read_text(),
            media_type=media,
            headers={"Cache-Control": "no-cache"},
        )

    @app.post("/session")
    def login(body: LoginRequest, response: Response, request: Request):
        if not hmac.compare_digest(body.token.encode(), settings.api_token.encode()):
            raise HTTPException(status_code=401, detail="invalid token")
        response.set_cookie(
            sessions.COOKIE_NAME,
            sessions.issue(settings.api_token),
            max_age=sessions.SESSION_SECONDS,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/",
        )
        return {"status": "ok"}

    @app.post("/logout")
    def logout(response: Response):
        response.delete_cookie(sessions.COOKIE_NAME, path="/")
        return {"status": "ok"}

    # ---- Protected: bearer token or session cookie required. ----

    protected = APIRouter(dependencies=[Depends(authenticate)])

    @protected.get("/meta")
    def meta():
        return {
            "version": VERSION,
            "search_configured": bool(settings.search_url),
            "model_configured": engine.reasoner.configured(),
        }

    @protected.post("/plan/investigation")
    def plan_investigation_route(body: InvestigationPlanRequest):
        try:
            plan = planning.plan_investigation(body.value, body.kind)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return asdict(plan)

    @protected.post("/plan/dataset")
    def plan_dataset_route(body: DatasetPlanRequest):
        try:
            plan = planning.plan_dataset(body.description, body.fields, body.seeds)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return asdict(plan)

    @protected.get("/health")
    def health():
        with engine.store.connection() as db:
            db.execute("SELECT 1")
        return {"status": "ok", "version": VERSION}

    @protected.post("/jobs", status_code=202)
    def submit(spec: JobSpec, idempotency_key: str | None = Header(default=None, max_length=200)):
        try:
            job = engine.submit(spec, idempotency_key)
        except ValueError as exc:
            raise HTTPException(
                status_code=409 if "idempotency" in str(exc) else 422, detail=str(exc)
            ) from exc
        return engine.store.job(job)

    @protected.post("/jobs/{job_id}/rerun", status_code=202)
    def rerun(job_id: str):
        try:
            new_id = engine.rerun(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return engine.store.job(new_id)

    @protected.post("/replays", status_code=202)
    def replay(
        spec: ReplaySpec, idempotency_key: str | None = Header(default=None, max_length=200)
    ):
        try:
            job = engine.replay(spec, idempotency_key)
        except ValueError as exc:
            raise HTTPException(
                status_code=409 if "idempotency" in str(exc) else 422, detail=str(exc)
            ) from exc
        return engine.store.job(job)

    @protected.get("/jobs/{job_id}/captures")
    def job_captures(
        job_id: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)
    ):
        return engine.store.captures(job_id, after, limit)

    @protected.get("/jobs/{job_id}/extractions")
    def extractions(
        job_id: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)
    ):
        return engine.store.extractions(job_id, after, limit)

    @protected.get("/jobs")
    def jobs(limit: int = Query(100, ge=1, le=1000)):
        return engine.store.jobs(limit)

    @protected.get("/jobs/{job_id}")
    def job(job_id: str):
        return engine.store.job(job_id)

    @protected.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        engine.store.stop(job_id)
        return engine.store.job(job_id)

    @protected.get("/jobs/{job_id}/events")
    def events(job_id: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)):
        return engine.store.events(job_id, after, limit)

    @protected.get("/jobs/{job_id}/dossier")
    def dossier(job_id: str):
        try:
            return engine.store.dossier(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @protected.get("/jobs/{job_id}/records")
    def records(job_id: str):
        return engine.store.job_records(job_id)

    @protected.get("/jobs/{job_id}/export.csv")
    def export_csv(job_id: str):
        rows = engine.store.job_records(job_id)
        return Response(
            render_csv(rows),
            media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="{job_id}.csv"'},
        )

    @protected.get("/jobs/{job_id}/tasks")
    def tasks(job_id: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)):
        engine.store.job(job_id)
        with engine.store.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,kind,key,depth,parent,reason,status,attempts,ready,error FROM tasks WHERE job_id=? AND id>? ORDER BY id LIMIT ?",
                    (job_id, after, limit),
                )
            ]

    @protected.get("/jobs/{job_id}/observations")
    def observations(job_id: str, after: str = "", limit: int = Query(100, ge=1, le=1000)):
        return engine.store.observations(job_id, after, limit)

    @protected.get("/datasets/{dataset}/entities")
    def entities(dataset: str, after: str = "", limit: int = Query(100, ge=1, le=1000)):
        return engine.store.canonical(dataset, after, limit)

    @protected.get("/jobs/{job_id}/export")
    def export(job_id: str):
        engine.store.job(job_id)

        def generate():
            after = ""
            while rows := engine.store.observations(job_id, after, 500):
                for row in rows:
                    yield json.dumps(row, ensure_ascii=False) + "\n"
                after = rows[-1]["id"]

        return StreamingResponse(
            generate(),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": f'attachment; filename="{job_id}.jsonl"'},
        )

    @protected.get("/captures/{capture_id}")
    def capture(capture_id: int):
        result = engine.store.capture(capture_id)
        result.pop("body")
        result["headers"] = json.loads(result["headers"])
        return result

    @protected.get("/captures/{capture_id}/body")
    def body(capture_id: int):
        result = engine.store.capture(capture_id)
        return Response(
            result["body"],
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f'attachment; filename="capture-{capture_id}.bin"',
                "X-Content-Type-Options": "nosniff",
            },
        )

    @protected.get("/schedules")
    def schedules():
        with engine.store.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,interval,next_run,last_job,enabled FROM schedules ORDER BY next_run LIMIT 1000"
                )
            ]

    @protected.post("/schedules/{schedule_id}/disable")
    def disable(schedule_id: str):
        engine.store.disable_schedule(schedule_id)
        return {"id": schedule_id, "enabled": False}

    app.include_router(protected)
    app.state.engine = engine
    return app
