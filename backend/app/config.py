"""Runtime configuration. Every limit is overridable through environment variables."""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _int(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    parsed = int(value)
    if parsed < 0:
        raise ValueError(f"{name} must be >= 0")
    return parsed


def _list(name: str, default: str) -> list[str]:
    return [item.strip() for item in os.environ.get(name, default).split(",") if item.strip()]


@dataclass(frozen=True)
class Settings:
    max_download_size_mb: int = field(default_factory=lambda: _int("MAX_DOWNLOAD_SIZE_MB", 200))
    max_playlist_items: int = field(default_factory=lambda: _int("MAX_PLAYLIST_ITEMS", 25))
    max_analyze_items: int = field(default_factory=lambda: _int("MAX_ANALYZE_ITEMS", 500))
    max_video_duration: int = field(default_factory=lambda: _int("MAX_VIDEO_DURATION", 1800))
    max_concurrent_jobs: int = field(default_factory=lambda: _int("MAX_CONCURRENT_JOBS", 1))
    max_queued_jobs: int = field(default_factory=lambda: _int("MAX_QUEUED_JOBS", 10))
    max_jobs_per_ip: int = field(default_factory=lambda: _int("MAX_ACTIVE_JOBS_PER_IP", 2))
    job_timeout: int = field(default_factory=lambda: _int("JOB_TIMEOUT", 900))
    analyze_timeout: int = field(default_factory=lambda: _int("ANALYZE_TIMEOUT", 60))
    file_ttl: int = field(default_factory=lambda: _int("FILE_TTL", 1800))
    rate_limit_per_minute: int = field(default_factory=lambda: _int("RATE_LIMIT_PER_MINUTE", 20))
    job_rate_limit_per_hour: int = field(default_factory=lambda: _int("JOB_RATE_LIMIT_PER_HOUR", 15))
    max_request_bytes: int = field(default_factory=lambda: _int("MAX_REQUEST_BYTES", 32768))
    work_dir: Path = field(default_factory=lambda: Path(os.environ.get("WORK_DIR", "/tmp/umd-work")))
    allowed_origins: list[str] = field(default_factory=lambda: _list("ALLOWED_ORIGINS", "*"))
    # Header holding the real client IP when running behind a trusted proxy (empty = socket peer).
    client_ip_header: str = field(default_factory=lambda: os.environ.get("CLIENT_IP_HEADER", "").lower())
    js_runtimes: list[str] = field(default_factory=lambda: _list("YTDLP_JS_RUNTIMES", "deno"))

    def public_limits(self) -> dict:
        return {
            "max_download_size_mb": self.max_download_size_mb,
            "max_playlist_items": self.max_playlist_items,
            "max_video_duration": self.max_video_duration,
            "max_concurrent_jobs": self.max_concurrent_jobs,
            "max_queued_jobs": self.max_queued_jobs,
            "job_timeout": self.job_timeout,
            "file_ttl": self.file_ttl,
            "rate_limit_per_minute": self.rate_limit_per_minute,
            "job_rate_limit_per_hour": self.job_rate_limit_per_hour,
        }


settings = Settings()
