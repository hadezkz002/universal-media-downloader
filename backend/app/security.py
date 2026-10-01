"""URL validation (SSRF protection), filename sanitizing and per-IP rate limiting."""

import http.client
import ipaddress
import logging
import re
import socket
import ssl
import threading
import time
import unicodedata
from collections import defaultdict, deque
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit

log = logging.getLogger("umd.security")

# Exact host allowlist. Anything else is rejected before yt-dlp ever sees the URL,
# so the service can never be used as an open proxy / SSRF primitive.
PLATFORM_HOSTS = {
    "youtube": {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"},
    "soundcloud": {"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com", "on.soundcloud.com", "api-v2.soundcloud.com"},
    "spotify": {"open.spotify.com"},
}

# yt-dlp extractors allowed to run (passed via --use-extractors). The generic extractor is excluded.
ALLOWED_EXTRACTORS = (
    "youtube,youtube:tab,youtube:playlist,youtube:user,YoutubeYtBe,youtube:search,"
    "soundcloud,soundcloud:set,soundcloud:playlist,soundcloud:user,soundcloud:user:permalink,soundcloud:search"
)

MAX_URL_LENGTH = 2048

# Share hosts that only redirect. yt-dlp has no extractor for them (only the generic one, which we
# never enable), so the backend resolves them itself, re-validating every hop.
SHORT_LINK_HOSTS = {"on.soundcloud.com"}
MAX_REDIRECTS = 5
REDIRECT_TIMEOUT = 10

# yt-dlp emits these for set items it has no permalink for; nothing else on this host is accepted.
_SC_API_TRACK = re.compile(r"^/tracks/\d+$")
_SC_TRACKING_PARAMS = {"ref", "p", "c", "si", "in", "in_system_playlist"}


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
    if platform is None or (host == "api-v2.soundcloud.com" and not _SC_API_TRACK.match(parts.path)):
        raise InvalidURL("Unsupported URL. Supported sites: YouTube, SoundCloud, Spotify (metadata only).")
    if resolve and not resolve_is_public(host):
        raise InvalidURL("This host resolves to a non-public address and is blocked.")
    return parts._replace(scheme="https", netloc=host, fragment="").geturl(), platform


def strip_tracking(url: str) -> str:
    """Drop SoundCloud share-tracking parameters; every other URL (e.g. YouTube ?list=) is untouched."""
    parts = urlsplit(url)
    if not (parts.hostname or "").endswith("soundcloud.com"):
        return url
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k not in _SC_TRACKING_PARAMS and not k.startswith("utm_")]
    return parts._replace(query=urlencode(query)).geturl()


def _head_pinned(url: str) -> tuple[int, str | None]:
    """HEAD request connecting to an IP we verified ourselves (no second DNS lookup => no rebinding)."""
    parts = urlsplit(url)
    host = parts.hostname
    infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    ips = [info[4][0] for info in infos]
    if not ips or not all(is_public_ip(ip) for ip in ips):
        raise InvalidURL("This host resolves to a non-public address and is blocked.")
    context = ssl.create_default_context()
    conn = http.client.HTTPSConnection(host, 443, timeout=REDIRECT_TIMEOUT, context=context)
    sock = socket.create_connection((ips[0], 443), timeout=REDIRECT_TIMEOUT)
    conn.sock = context.wrap_socket(sock, server_hostname=host)
    try:
        conn.request("HEAD", (parts.path or "/") + (f"?{parts.query}" if parts.query else ""),
                     headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
        resp = conn.getresponse()
        return resp.status, resp.getheader("Location")
    finally:
        conn.close()


def resolve_short_link(url: str) -> str:
    """Follow redirects of a share link hop by hop; each hop must pass validate_url again."""
    current = url
    for _ in range(MAX_REDIRECTS):
        try:
            status, location = _head_pinned(current)
        except (OSError, http.client.HTTPException) as exc:
            log.warning("short link resolve failed url=%s err=%r", current, exc)
            raise InvalidURL("Could not resolve SoundCloud shared link (network error).") from None
        if status in (301, 302, 303, 307, 308) and location:
            current, _platform = validate_url(urljoin(current, location))
            if urlsplit(current).hostname not in SHORT_LINK_HOSTS:
                return strip_tracking(current)
            continue
        log.warning("short link did not redirect url=%s status=%s", current, status)
        if status == 404:
            raise InvalidURL("This SoundCloud shared link is invalid or has expired.")
        raise InvalidURL("Could not resolve SoundCloud shared link.")
    raise InvalidURL("Could not resolve SoundCloud shared link (too many redirects).")


_URL_LEADING = "\"'<([{“‘«"
_URL_TRAILING = "\"'>]}”’».,;:!?…"


def normalize_input(raw: str) -> str:
    """Pick the first http(s) URL out of pasted share text and strip quotes / trailing punctuation."""
    text = (raw or "").strip()
    for token in text.split():
        token = token.lstrip(_URL_LEADING)
        if token.lower().startswith(("http://", "https://")):
            # Trailing ")" is kept only when it closes a "(" inside the URL (e.g. Wikipedia titles).
            while token and (token[-1] in _URL_TRAILING or (token[-1] == ")" and token.count(")") > token.count("("))):
                token = token[:-1]
            return token
    return text


def canonicalize(raw: str) -> tuple[str, str]:
    """normalize -> validate -> resolve share-link redirects safely -> canonical URL. Returns (url, platform)."""
    url, platform = validate_url(normalize_input(raw))
    if urlsplit(url).hostname in SHORT_LINK_HOSTS:
        url = resolve_short_link(url)
    return strip_tracking(url), platform


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
