"""HTTP API for Universal Media Downloader."""

import logging
import re
import shutil
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from yt_dlp.version import __version__ as ytdlp_version

from . import media
from .config import settings
from .jobs import Busy, Item, JobManager
from .security import InvalidURL, RateLimiter, validate_url

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger("umd.api")

jobs = JobManager()
limiter = RateLimiter()
JOB_ID = re.compile(r"^[A-Za-z0-9_-]{16,64}$")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    shutil.rmtree(settings.work_dir, ignore_errors=True)  # media never survives a restart
    settings.work_dir.mkdir(parents=True, exist_ok=True)
    jobs.start_cleanup()
    yield


app = FastAPI(title="Universal Media Downloader API", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=settings.allowed_origins, allow_methods=["GET", "POST", "DELETE"],
                   allow_headers=["Content-Type"], max_age=600)


class BodyLimit:
    """Reject request bodies above MAX_REQUEST_BYTES, including chunked uploads."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        limit = settings.max_request_bytes
        headers = dict(scope.get("headers") or [])
        if int(headers.get(b"content-length", b"0") or 0) > limit:
            return await JSONResponse({"error": "Request too large."}, 413)(scope, receive, send)
        seen = 0

        async def limited_receive():
            nonlocal seen
            message = await receive()
            seen += len(message.get("body", b""))
            if seen > limit:
                raise HTTPException(413, "Request too large.")
            return message

        return await self.app(scope, limited_receive, send)


app.add_middleware(BodyLimit)


@app.exception_handler(HTTPException)
async def http_error(_req: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, exc.status_code)


@app.exception_handler(RequestValidationError)
async def validation_error(_req: Request, exc: RequestValidationError):
    return JSONResponse({"error": "Invalid request."}, 422)


@app.exception_handler(Exception)
async def unhandled_error(req: Request, exc: Exception):
    log.exception("unhandled error path=%s", req.url.path)
    return JSONResponse({"error": "Unexpected server error."}, 500)


def client_ip(request: Request) -> str:
    header = settings.client_ip_header
    if header and (value := request.headers.get(header)):
        return value.split(",")[0].strip()[:64]
    return request.client.host if request.client else "unknown"


def rate_limit(request: Request, bucket: str, limit: int, window: float) -> str:
    ip = client_ip(request)
    if not limiter.allow(ip, bucket, limit, window):
        raise HTTPException(429, "Too many requests. Please wait a moment and try again.")
    return ip


def checked_url(raw: str) -> tuple[str, str]:
    try:
        return validate_url(raw)
    except InvalidURL as exc:
        raise HTTPException(400, str(exc)) from None


# ------------------------------------------------------------------ endpoints

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "yt_dlp": ytdlp_version,
        "ffmpeg": bool(shutil.which("ffmpeg")),
        "js_runtime": [r for r in settings.js_runtimes if shutil.which(r)],
        "active_jobs": sum(1 for j in list(jobs.jobs.values()) if j.finished is None),
        "limits": settings.public_limits(),
    }


class AnalyzeRequest(BaseModel):
    url: str = Field(max_length=2048)


@app.post("/api/analyze")
async def analyze(body: AnalyzeRequest, request: Request):
    rate_limit(request, "analyze", settings.rate_limit_per_minute, 60)
    url, platform = await run_in_threadpool(checked_url, body.url)
    try:
        result = await run_in_threadpool(media.analyze, url, platform)
    except media.MediaError as exc:
        raise HTTPException(422, str(exc)) from None
    result["limits"] = settings.public_limits()
    return result


class ResolveRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    artist: str = Field(default="", max_length=200)


@app.post("/api/resolve")
async def resolve(body: ResolveRequest, request: Request):
    """Spotify resolver: search public sources for a track. Never downloads from Spotify."""
    rate_limit(request, "analyze", settings.rate_limit_per_minute, 60)
    query = " ".join(f"{body.artist} {body.title}".split())
    candidates = await run_in_threadpool(media.search_candidates, query)
    return {"query": query, "candidates": candidates}


class JobRequest(BaseModel):
    url: str = Field(max_length=2048)
    mode: Literal["audio", "video"]
    format: str = Field(max_length=10)
    quality: str = Field(default="best", max_length=10)
    items: list[str] | None = Field(default=None, max_length=500)
    playlist_title: str | None = Field(default=None, max_length=300)


@app.post("/api/jobs", status_code=201)
async def create_job(body: JobRequest, request: Request):
    ip = rate_limit(request, "jobs", settings.job_rate_limit_per_hour, 3600)
    url, platform = await run_in_threadpool(checked_url, body.url)
    if platform == "spotify":
        raise HTTPException(400, "Spotify audio cannot be downloaded. Use 'Find source' to pick a public source.")
    if body.items is not None:
        if not body.items:
            raise HTTPException(400, "Select at least one item.")
        if len(body.items) > settings.max_playlist_items:
            raise HTTPException(400, f"Public server limit: {settings.max_playlist_items} items per job. "
                                     f"You selected {len(body.items)}.")
        urls = [await run_in_threadpool(checked_url, u) for u in body.items]
        if any(p == "spotify" for _, p in urls):
            raise HTTPException(400, "Spotify items cannot be downloaded directly.")
        items = [Item(u) for u, _ in urls]
    else:
        items = [Item(url)]
    try:
        job = jobs.create(ip, body.mode, body.format, body.quality, items, body.playlist_title,
                          numbered=body.items is not None)
    except Busy as exc:
        raise HTTPException(503, str(exc)) from None
    except media.MediaError as exc:
        raise HTTPException(400, str(exc)) from None
    return job.public()


def _job(job_id: str):
    job = jobs.get(job_id) if JOB_ID.match(job_id) else None
    if not job:
        raise HTTPException(404, "Job not found or expired.")
    return job


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    return _job(job_id).public()


@app.get("/api/jobs/{job_id}/files/{index}")
def job_file(job_id: str, index: int):
    job = _job(job_id)
    if not 0 <= index < len(job.items) or job.items[index].status != "ready":
        raise HTTPException(404, "File not available.")
    item = job.items[index]
    # Paths come from the server-side job record only; the client never supplies a path.
    return FileResponse(item.path, filename=item.name, media_type="application/octet-stream")


@app.get("/api/jobs/{job_id}/zip")
def job_zip(job_id: str):
    job = _job(job_id)
    if not job.zip_path or job.status != "ready":
        raise HTTPException(404, "ZIP not available.")
    return FileResponse(job.zip_path, filename=job.zip_name, media_type="application/zip")


@app.delete("/api/jobs/{job_id}")
def delete_job(job_id: str):
    _job(job_id)
    jobs.delete(job_id)
    return {"deleted": True}
