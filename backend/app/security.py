"""URL validation (SSRF protection), filename sanitizing and per-IP rate limiting."""

import ipaddress
import re
import socket
import threading
import time
import unicodedata
from collections import defaultdict, deque
from urllib.parse import urlsplit

# Exact host allowlist. Anything else is rejected before yt-dlp ever sees the URL,
# so the service can never be used as an open proxy / SSRF primitive.
PLATFORM_HOSTS = {
    "youtube": {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"},
    "soundcloud": {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com", "on.soundcloud.com"},
    "spotify": {"open.spotify.com"},
}

# yt-dlp extractors allowed to run (passed via --use-extractors). The generic extractor is excluded.
ALLOWED_EXTRACTORS = (
    "youtube,youtube:tab,youtube:playlist,youtube:user,YoutubeYtBe,youtube:search,"
    "soundcloud,soundcloud:set,soundcloud:playlist,soundcloud:user,soundcloud:user:permalink,soundcloud:search"
)

MAX_URL_LENGTH = 2048


class InvalidURL(ValueError):
    """Raised with a user-safe message."""


def is_public_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


def resolve_is_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False
    return bool(infos) and all(is_public_ip(info[4][0]) for info in infos)


def detect_platform(host: str) -> str | None:
    for platform, hosts in PLATFORM_HOSTS.items():
        if host in hosts:
            return platform
    return None


def validate_url(raw: str, resolve: bool = True) -> tuple[str, str]:
    """Return (normalized_url, platform) or raise InvalidURL."""
    url = (raw or "").strip()
    if not url or len(url) > MAX_URL_LENGTH or any(c.isspace() or ord(c) < 32 for c in url):
        raise InvalidURL("Please paste a valid http(s) URL.")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise InvalidURL("Only http and https URLs are allowed.")
    if parts.username or parts.password:
        raise InvalidURL("URLs with embedded credentials are not allowed.")
    try:
        port = parts.port
    except ValueError:
        raise InvalidURL("Invalid port in URL.") from None
    if port not in (None, 80, 443):
        raise InvalidURL("Custom ports are not allowed.")
    host = (parts.hostname or "").rstrip(".").lower()
    platform = detect_platform(host)
    if platform is None:
        raise InvalidURL("Unsupported URL. Supported sites: YouTube, SoundCloud, Spotify (metadata only).")
    if resolve and not resolve_is_public(host):
        raise InvalidURL("This host resolves to a non-public address and is blocked.")
    return parts._replace(scheme="https", netloc=host, fragment="").geturl(), platform


_FORBIDDEN_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def sanitize_filename(name: str, ext: str = "", max_bytes: int = 180) -> str:
    """Keep Unicode (Vietnamese, CJK, emoji) but strip path separators and unsafe characters."""
    name = unicodedata.normalize("NFC", name or "")
    name = "".join(c for c in name if unicodedata.category(c) not in ("Cc", "Cf", "Cs", "Co") or c == "\u200d")
    name = _FORBIDDEN_CHARS.sub("_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    ext = re.sub(r"[^A-Za-z0-9]", "", ext or "")[:10]
    suffix = f".{ext}" if ext else ""
    budget = max_bytes - len(suffix.encode())
    encoded = name.encode("utf-8")
    if len(encoded) > budget:
        name = encoded[:budget].decode("utf-8", errors="ignore").rstrip(" .")
    if not name or name.split(".")[0].upper() in _WINDOWS_RESERVED:
        name = f"_{name}" if name else "download"
    return name + suffix


class RateLimiter:
    """Sliding-window limiter kept in memory.

    ponytail: in-process state, fine for the single free-tier instance; move to Redis if scaled out.
    """

    def __init__(self) -> None:
        self._hits: dict[tuple[str, str], deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, bucket: str, limit: int, window: float) -> bool:
        if limit <= 0:
            return True
        now = time.monotonic()
        with self._lock:
            hits = self._hits[(key, bucket)]
            while hits and hits[0] <= now - window:
                hits.popleft()
            if len(hits) >= limit:
                return False
            hits.append(now)
            if len(self._hits) > 50_000:
                self._hits = defaultdict(deque, {k: v for k, v in self._hits.items() if v})
            return True
