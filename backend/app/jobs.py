"""In-memory job queue with bounded concurrency and TTL cleanup.

ponytail: single-process state; a restart drops jobs (clients just retry). Needs shared
storage (Redis + object store) only if the service is ever scaled past one instance.
"""

import logging
import secrets
import shutil
import subprocess
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import media
from .config import settings
from .security import sanitize_filename

log = logging.getLogger("umd.jobs")

ACTIVE = ("queued", "downloading", "processing", "merging", "zipping")


class Busy(Exception):
    """Raised when the queue or per-IP quota is full (message is user-safe)."""


@dataclass
class Item:
    url: str
    title: str | None = None
    status: str = "queued"
    error: str | None = None
    name: str | None = None
    path: Path | None = None
    size: int | None = None


@dataclass
class Job:
    id: str
    ip: str
    mode: str
    fmt: str
    quality: str
    items: list[Item]
    playlist_title: str | None = None
    numbered: bool = False
    status: str = "queued"
    progress: float = 0.0
    error: str | None = None
    created: float = field(default_factory=time.time)
    finished: float | None = None
    zip_path: Path | None = None
    zip_name: str | None = None
    cancelled: threading.Event = field(default_factory=threading.Event)
    proc: subprocess.Popen | None = None

    @property
    def dir(self) -> Path:
        return settings.work_dir / self.id

    def public(self) -> dict:
        return {
            "id": self.id, "status": self.status, "progress": round(self.progress, 1), "error": self.error,
            "mode": self.mode, "format": self.fmt, "quality": self.quality,
            "expires_at": self.finished + settings.file_ttl if self.finished else None,
            "counts": {state: sum(1 for it in self.items if it.status == state) for state in ("ready", "failed", "skipped")},
            "items": [{"index": i, "title": it.title, "status": it.status, "error": it.error, "name": it.name,
                       "size": it.size} for i, it in enumerate(self.items)],
            "zip": {"name": self.zip_name, "size": self.zip_path.stat().st_size} if self.zip_path else None,
        }


class JobManager:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=max(1, settings.max_concurrent_jobs), thread_name_prefix="job")

    # -------------------------------------------------------------- lifecycle
    def create(self, ip: str, mode: str, fmt: str, quality: str, items: list[Item],
               playlist_title: str | None, numbered: bool) -> Job:
        media.format_args(mode, fmt, quality)  # validates the enum combination
        with self.lock:
            active = [j for j in self.jobs.values() if j.status in ACTIVE]
            if len(active) >= settings.max_queued_jobs:
                raise Busy("Server is temporarily busy. Try again later.")
            if sum(1 for j in active if j.ip == ip) >= settings.max_jobs_per_ip:
                raise Busy("You already have downloads in progress. Wait for them to finish.")
            job = Job(secrets.token_urlsafe(16), ip, mode, fmt, quality, items, playlist_title, numbered)
            self.jobs[job.id] = job
        self.pool.submit(self._run, job)
        log.info("job created id=%s ip=%s items=%d mode=%s fmt=%s q=%s", job.id, ip, len(items), mode, fmt, quality)
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def delete(self, job_id: str) -> bool:
        with self.lock:
            job = self.jobs.pop(job_id, None)
        if not job:
            return False
        job.cancelled.set()
        if job.proc and job.proc.poll() is None:
            media._kill(job.proc)
        shutil.rmtree(job.dir, ignore_errors=True)
        return True

    # -------------------------------------------------------------- worker
    def _run(self, job: Job) -> None:
        deadline = time.monotonic() + settings.job_timeout
        total = len(job.items)
        width = max(2, len(str(total)))
        try:
            for n, item in enumerate(job.items):
                if job.cancelled.is_set():
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 5:
                    item.status, item.error = "skipped", "Skipped: job time limit reached."
                    continue

                def progress(stage: str, frac: float | None, n=n, item=item) -> None:
                    item.status = job.status = stage
                    if frac is not None:
                        job.progress = 100 * (n + frac * 0.9) / total

                item.status = job.status = "downloading"
                try:
                    info = media.download(item.url, job.mode, job.fmt, job.quality, job.dir, n, remaining, progress,
                                          register=lambda p: setattr(job, "proc", p),
                                          track=n + 1 if job.numbered else None,
                                          album=job.playlist_title if job.numbered and job.mode == "audio" else None)
                except media.MediaError as exc:
                    item.status, item.error = "failed", str(exc)
                    continue
                except Exception:
                    log.exception("unexpected download error job=%s url=%s", job.id, item.url)
                    item.status, item.error = "failed", "Unexpected server error."
                    continue
                item.path = Path(info["filepath"])
                item.size = item.path.stat().st_size
                item.title = info.get("title") or item.title
                item.name = media.output_name(info, job.mode, n + 1 if job.numbered else None, width)
                item.status = "ready"
                job.progress = 100 * (n + 1) / total
            if job.cancelled.is_set():
                return
            done = [it for it in job.items if it.status == "ready"]
            if not done:
                job.status, job.error = "failed", job.items[0].error if total == 1 else f"No item could be downloaded ({job.items[0].error})"
                return
            if len(done) > 1:
                job.status = "zipping"
                self._zip(job, done)
            job.status, job.progress = "ready", 100.0
        except Exception:
            log.exception("job crashed id=%s", job.id)
            job.status, job.error = "failed", "Unexpected server error."
        finally:
            job.proc = None
            job.finished = time.time()
            log.info("job finished id=%s status=%s", job.id, job.status)

    @staticmethod
    def _zip(job: Job, items: list[Item]) -> None:
        used: set[str] = set()
        path = job.dir / "bundle.zip"
        # Media is already compressed; ZIP_STORED keeps CPU usage minimal on the free tier.
        with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
            for it in items:
                name, stem, k = it.name, Path(it.name).stem, 1
                while name.lower() in used:
                    k += 1
                    name = f"{stem} ({k}){Path(it.name).suffix}"
                used.add(name.lower())
                zf.write(it.path, arcname=name)
        job.zip_path = path
        job.zip_name = sanitize_filename(job.playlist_title or "playlist", "zip")

    # -------------------------------------------------------------- cleanup
    def cleanup_once(self) -> None:
        now = time.time()
        expired = [j.id for j in list(self.jobs.values())
                   if (j.finished and now - j.finished > settings.file_ttl)
                   or (not j.finished and now - j.created > settings.job_timeout + settings.file_ttl)]
        for job_id in expired:
            self.delete(job_id)
        # Remove orphaned directories (e.g. left behind by a crash).
        if settings.work_dir.exists():
            for d in settings.work_dir.iterdir():
                if d.name not in self.jobs and now - d.stat().st_mtime > 60:
                    shutil.rmtree(d, ignore_errors=True)
        if expired:
            log.info("cleanup removed %d expired jobs", len(expired))

    def start_cleanup(self, interval: float = 60) -> None:
        def loop() -> None:
            while True:
                time.sleep(interval)
                try:
                    self.cleanup_once()
                except Exception:
                    log.exception("cleanup failed")

        threading.Thread(target=loop, daemon=True, name="cleanup").start()
