"""HTTP boundary, validation, authentication, and embedded worker lifecycle."""

import asyncio
import hmac
import json
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from threading import Event, Thread
from typing import Annotated, Literal

from fastapi import APIRouter, FastAPI, Header, HTTPException, Query, Response, Security
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.security import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from queuepilot.config import Settings
from queuepilot.store import STATUSES, Conflict, NotFound, Store
from queuepilot.worker import Worker

JobStatus = Literal["queued", "running", "retry_wait", "succeeded", "failed", "cancelled"]


class JobInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=20000)]
    max_attempts: Annotated[int, Field(strict=True, ge=1, le=5)] = 3
    simulate_failures: Annotated[int, Field(strict=True, ge=0, le=4)] = 0


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    store = Store(settings.db_path)
    worker = Worker(store, settings)

    @asynccontextmanager
    async def lifespan(app):
        stop, thread = Event(), None
        if settings.worker_enabled:
            thread = Thread(target=worker.run, args=(stop,), daemon=True, name="queuepilot-worker")
            thread.start()
        try:
            yield
        finally:
            stop.set()
            if thread is not None:
                await asyncio.to_thread(thread.join, 31)

    app = FastAPI(
        title="QueuePilot",
        version="0.1.0",
        lifespan=lifespan,
        description="可靠 AI 摘要任务服务：幂等提交、自动重试、租约恢复与执行历史。",
    )
    app.state.store, app.state.settings = store, settings
    key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

    async def authorize(key: Annotated[str | None, Security(key_header)]):
        if settings.api_key and (
            key is None
            or not hmac.compare_digest(key.encode("utf-8"), settings.api_key.encode("utf-8"))
        ):
            raise HTTPException(401, "缺少或无效的 API 密钥")

    router = APIRouter(prefix="/api", dependencies=[Security(authorize)])

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        # Do not echo raw input/exception objects; escaped JSON is safe even for
        # invalid surrogate code points in the caller's original JSON.
        errors = [{name: error[name] for name in ("loc", "msg", "type")} for error in exc.errors()]
        return Response(
            json.dumps({"detail": errors}, ensure_ascii=True),
            status_code=422,
            media_type="application/json",
        )

    @app.exception_handler(NotFound)
    async def not_found(request, exc):
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(Conflict)
    async def conflict(request, exc):
        return JSONResponse(status_code=409, content={"detail": str(exc)})

    @app.exception_handler(sqlite3.Error)
    async def database_unavailable(request, exc):
        return JSONResponse(status_code=503, content={"detail": "数据库暂时不可用"})

    @router.post("/jobs", status_code=202)
    def submit(
        body: JobInput, response: Response, idempotency_key: Annotated[str | None, Header()] = None
    ):
        if idempotency_key is not None and not re.fullmatch(
            r"[A-Za-z0-9._:-]{1,128}", idempotency_key
        ):
            raise HTTPException(422, "Idempotency-Key 必须为 1–128 个字母、数字或 . _ : -")
        if settings.provider != "mock" and body.simulate_failures:
            raise HTTPException(422, "模拟失败仅适用于 mock 模式")
        job, created = store.submit(body.model_dump(), idempotency_key)
        response.status_code = 202 if created else 200
        response.headers["Location"] = "/api/jobs/" + job["id"]
        return job

    @router.get("/jobs")
    def list_jobs(
        status: JobStatus | None = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 20,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        return store.list_jobs(status, limit, offset)

    @router.get("/jobs/{job_id}")
    def get_job(job_id: str):
        return store.get(job_id)

    @router.get("/jobs/{job_id}/events")
    def events(job_id: str, after: Annotated[int, Query(ge=0)] = 0):
        return {"items": store.events(job_id, after)}

    @router.post("/jobs/{job_id}/retry")
    def retry(job_id: str):
        return store.retry(job_id)

    @router.post("/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        return store.cancel(job_id)

    @router.get("/stats")
    def stats():
        return {
            **store.stats(),
            "provider": settings.provider,
            "worker_enabled": settings.worker_enabled,
            "poll_seconds": settings.poll_seconds,
        }

    @app.get("/metrics", dependencies=[Security(authorize)], response_class=PlainTextResponse)
    def metrics():
        snapshot = store.stats()
        lines = ["# HELP queuepilot_jobs Jobs by current status.", "# TYPE queuepilot_jobs gauge"]
        lines.extend(
            f'queuepilot_jobs{{status="{status}"}} {snapshot["counts"][status]}'
            for status in STATUSES
        )
        lines.extend(
            [
                "# HELP queuepilot_attempts_total Persisted execution attempts.",
                "# TYPE queuepilot_attempts_total counter",
                f"queuepilot_attempts_total {snapshot['total_attempts']}",
            ]
        )
        return PlainTextResponse(
            "\n".join(lines) + "\n", media_type="text/plain; version=0.0.4; charset=utf-8"
        )

    @app.get("/healthz")
    def health():
        store.stats()
        return {"status": "ok"}

    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir, check_dir=False), name="static")

    @app.get("/", include_in_schema=False)
    def console():
        return FileResponse(static_dir / "index.html")

    app.include_router(router)
    return app
