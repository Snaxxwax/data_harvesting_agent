from __future__ import annotations

import hmac
import json

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Response
from fastapi.responses import StreamingResponse

from .config import Settings
from .engine import Engine
from .models import JobSpec, ReplaySpec


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    if len(settings.api_token) < 24 or settings.api_token.startswith("replace-this"):
        raise RuntimeError("HARVEST_API_TOKEN must be set to at least 24 characters")
    engine = Engine(settings)

    def authenticate(authorization: str | None = Header(default=None)):
        expected = "Bearer " + settings.api_token
        if not authorization or not hmac.compare_digest(authorization.encode(), expected.encode()):
            raise HTTPException(status_code=401, detail="invalid bearer token")

    app = FastAPI(
        title="Harvest Platform",
        version="0.3.0",
        description="Durable jobs, evidence and source observations. Run a separate harvest worker.",
        dependencies=[Depends(authenticate)],
    )

    @app.exception_handler(KeyError)
    async def not_found(request, exc):
        return Response(
            content='{"detail":"not found"}', media_type="application/json", status_code=404
        )

    @app.get("/health")
    def health():
        with engine.store.connection() as db:
            db.execute("SELECT 1")
        return {"status": "ok", "version": "0.3.0"}

    @app.post("/jobs", status_code=202)
    def submit(spec: JobSpec, idempotency_key: str | None = Header(default=None, max_length=200)):
        try:
            job = engine.submit(spec, idempotency_key)
        except ValueError as exc:
            raise HTTPException(
                status_code=409 if "idempotency" in str(exc) else 422, detail=str(exc)
            ) from exc
        return engine.store.job(job)

    @app.post("/replays", status_code=202)
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

    @app.get("/jobs/{job_id}/captures")
    def job_captures(
        job_id: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)
    ):
        return engine.store.captures(job_id, after, limit)

    @app.get("/jobs/{job_id}/extractions")
    def extractions(
        job_id: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)
    ):
        return engine.store.extractions(job_id, after, limit)

    @app.get("/jobs")
    def jobs(limit: int = Query(100, ge=1, le=1000)):
        return engine.store.jobs(limit)

    @app.get("/jobs/{job_id}")
    def job(job_id: str):
        return engine.store.job(job_id)

    @app.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        engine.store.stop(job_id)
        return engine.store.job(job_id)

    @app.get("/jobs/{job_id}/events")
    def events(job_id: str, after: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)):
        return engine.store.events(job_id, after, limit)

    @app.get("/jobs/{job_id}/tasks")
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

    @app.get("/jobs/{job_id}/observations")
    def observations(job_id: str, after: str = "", limit: int = Query(100, ge=1, le=1000)):
        return engine.store.observations(job_id, after, limit)

    @app.get("/datasets/{dataset}/entities")
    def entities(dataset: str, after: str = "", limit: int = Query(100, ge=1, le=1000)):
        return engine.store.canonical(dataset, after, limit)

    @app.get("/jobs/{job_id}/export")
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

    @app.get("/captures/{capture_id}")
    def capture(capture_id: int):
        result = engine.store.capture(capture_id)
        result.pop("body")
        result["headers"] = json.loads(result["headers"])
        return result

    @app.get("/captures/{capture_id}/body")
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

    @app.get("/schedules")
    def schedules():
        with engine.store.connection() as db:
            return [
                dict(r)
                for r in db.execute(
                    "SELECT id,interval,next_run,last_job,enabled FROM schedules ORDER BY next_run LIMIT 1000"
                )
            ]

    @app.post("/schedules/{schedule_id}/disable")
    def disable(schedule_id: str):
        engine.store.disable_schedule(schedule_id)
        return {"id": schedule_id, "enabled": False}

    app.state.engine = engine
    return app
