"""Thin wrappers around the yt-dlp CLI plus the Spotify metadata reader.

yt-dlp always runs as a subprocess with an argument list (never a shell), with a fixed
set of options built here. Clients only choose from enumerated formats/qualities.
"""

import json
import logging
import os
import re
import shlex
import signal
import subprocess
import sys
import threading
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit

from .config import settings
from .security import ALLOWED_EXTRACTORS, sanitize_filename

log = logging.getLogger("umd.media")

AUDIO_FORMATS = ("mp3", "m4a", "opus", "flac", "original")
VIDEO_FORMATS = ("mp4", "webm")
VIDEO_QUALITIES = ("best", "1080", "720", "480", "360")


class MediaError(Exception):
    """Error whose message is safe to show to end users."""


# (pattern in yt-dlp output, user-facing message). First match wins.
_ERROR_MAP = [
    (r"confirm you.?re not a bot|Sign in to confirm", "YouTube blocked this server with a bot check. This public service does not use accounts or cookies, so it cannot process this video right now. Try again later or try SoundCloud."),
    (r"DRM", "This media is DRM-protected and cannot be downloaded."),
    (r"age.?restrict|confirm your age|inappropriate for some users", "Age-restricted content requires sign-in and cannot be processed by this public service."),
    (r"Private video|members.only|Join this channel|requires? (authentication|login|sign.?in)|login required|--cookies|account cookies", "This source requires authentication and cannot be processed by this public service."),
    (r"not (made )?available in your country|geo.?restrict", "This media is not available in the server's region (geo-restricted)."),
    (r"is live|live event|livestream|is_live", "Live streams are not supported."),
    (r"larger than max-filesize|File is larger", f"File exceeds the server limit of {settings.max_download_size_mb} MB."),
    (r"does not pass filter", f"Media is longer than the server limit of {settings.max_video_duration // 60} minutes."),
    (r"Unsupported URL|no suitable extractor", "Unsupported URL (no supported extractor for this link)."),
    (r"Requested format is not available", "The requested format/quality is not available for this media."),
    (r"Video unavailable|has been removed|does not exist|HTTP Error 404|not available", "Media unavailable."),
    (r"\[soundcloud[^]]*\].*(HTTP Error 429|Too Many Requests)", "SoundCloud temporarily blocked the request. Try again later."),
    (r"HTTP Error 429|Too Many Requests", "The source site is rate-limiting this server. Try again later."),
    (r"ExtractorError|Unable to extract", "yt-dlp extractor failed for this link. Try again later."),
]


def friendly_error(output: str) -> str:
    for pattern, message in _ERROR_MAP:
        if re.search(pattern, output, re.IGNORECASE):
            return message
    return "Media unavailable or could not be processed."


def base_args() -> list[str]:
    args = [sys.executable, str(Path(__file__).with_name("ytdlp_cli.py")), "--ignore-config", "--no-cache-dir", "--use-extractors", ALLOWED_EXTRACTORS,
            "--socket-timeout", "20", "--retries", "3", "--no-mtime", "--no-color"]
    for runtime in settings.js_runtimes:
        args += ["--js-runtimes", runtime]
    return args


def run_ytdlp(args: list[str], timeout: float, on_line: Callable[[str], None] | None = None,
              register: Callable[[subprocess.Popen], None] | None = None) -> tuple[int, str, str]:
    """Run yt-dlp; kill the whole process group (incl. ffmpeg) on timeout."""
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace", start_new_session=True)
    if register:
        register(proc)
    timed_out = threading.Event()
    stderr_chunks: list[str] = []
    err_reader = threading.Thread(target=lambda: stderr_chunks.append(proc.stderr.read()), daemon=True)
    err_reader.start()
    timer = threading.Timer(max(timeout, 1), lambda: (timed_out.set(), _kill(proc)))
    timer.start()
    stdout_lines: list[str] = []
    try:
        for line in proc.stdout:
            stdout_lines.append(line)
            if on_line:
                on_line(line.rstrip("\n"))
        proc.wait()
    finally:
        timer.cancel()
        err_reader.join(5)
    if timed_out.is_set():
        raise MediaError("Processing timed out on the server.")
    return proc.returncode, "".join(stdout_lines), "".join(stderr_chunks)


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _ytdlp_json(url: str, extra: list[str], timeout: float) -> dict:
    code, out, err = run_ytdlp(base_args() + ["-J", *extra, "--", url], timeout)
    if code != 0 or not out.strip():
        log.warning("yt-dlp analyze failed url=%s code=%s stderr=%s", url, code, err[-2000:])
        raise MediaError(friendly_error(err))
    return json.loads(out)


def _thumb(info: dict) -> str | None:
    if info.get("thumbnail"):
        return info["thumbnail"]
    thumbs = [t for t in info.get("thumbnails") or [] if t.get("url")]
    return thumbs[-1]["url"] if thumbs else None


def _size(fmt: dict | None) -> int | None:
    return (fmt or {}).get("filesize") or (fmt or {}).get("filesize_approx")


def _format_summary(info: dict) -> dict:
    """Approximate sizes for each option the UI offers."""
    formats = info.get("formats") or []
    audio = [f for f in formats if f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")]
    video = [f for f in formats if f.get("vcodec") not in (None, "none") and f.get("height")]
    best_audio = max(audio, key=lambda f: f.get("abr") or f.get("tbr") or 0, default=None)
    heights = sorted({f["height"] for f in video}, reverse=True)
    video_sizes = {}
    for q in VIDEO_QUALITIES[1:]:
        candidates = [f for f in video if f["height"] <= int(q)]
        if candidates:
            best = max(candidates, key=lambda f: (f["height"], f.get("ext") == "mp4", f.get("tbr") or 0))
            size = _size(best)
            if size and best.get("acodec") in (None, "none"):
                size += _size(best_audio) or 0
            video_sizes[q] = size
    return {
        "has_video": bool(video),
        "has_audio": bool(audio) or any(f.get("acodec") not in (None, "none") for f in formats),
        "max_height": heights[0] if heights else None,
        "heights": heights,
        "has_webm": any(f.get("ext") == "webm" for f in video),
        "audio_size": _size(best_audio),
        "video_sizes": video_sizes,
    }


def _entry_title(entry: dict) -> str:
    if entry.get("title"):
        return entry["title"]
    # SoundCloud flat entries carry only a URL; derive a readable placeholder from the slug.
    slug = urlsplit(entry.get("url") or "").path.rstrip("/").rsplit("/", 1)[-1]
    return slug.replace("-", " ").strip().title() or "Untitled"


_SC_POLICY_UNAVAILABLE = {"BLOCK": "Blocked by SoundCloud in the server's region.",
                          "SNIP": "SoundCloud Go+ track (only a preview is public)."}
_YT_UNAVAILABLE_TITLES = {"[Private video]", "[Deleted video]", "[Unavailable video]"}


def _entry_unavailable(e: dict) -> str | None:
    if reason := _SC_POLICY_UNAVAILABLE.get(e.get("sc_policy") or ""):
        return reason
    if e.get("title") in _YT_UNAVAILABLE_TITLES or e.get("availability") in ("private", "needs_auth", "subscriber_only", "premium_only"):
        return "Private, deleted or requires sign-in."
    if e.get("duration") and e["duration"] > settings.max_video_duration:
        return f"Longer than the server limit ({settings.max_video_duration // 60} min)."
    return None


def media_type(platform: str, info: dict, url: str) -> str:
    path = urlsplit(url).path
    if not (info.get("_type") == "playlist" or "entries" in info):
        return "shorts" if "/shorts/" in path else "video" if platform == "youtube" else "track"
    if platform == "soundcloud":
        if "user" in (info.get("extractor") or ""):
            return "profile"
        return "album" if info.get("album_type") == "album" else "playlist"
    return "channel" if re.match(r"^/(@|channel/|c/|user/)", path) else "playlist"


def analyze(url: str, platform: str) -> dict:
    if platform == "spotify":
        return spotify_metadata(url)
    info = _ytdlp_json(url, ["--flat-playlist", "--playlist-items", f"1:{settings.max_analyze_items}"],
                       settings.analyze_timeout)
    # A channel root returns its tabs (Videos, Live, Shorts) as nested playlists; use the first tab.
    entries = info.get("entries") or []
    if entries and all((e or {}).get("_type") == "playlist" for e in entries):
        tab = entries[0]
        info = {**info, "entries": tab.get("entries") or [], "playlist_count": len(tab.get("entries") or []),
                "title": tab.get("title") or info.get("title")}
    result = {
        "platform": platform,
        "media_type": media_type(platform, info, url),
        "title": info.get("title") or "Untitled",
        "uploader": info.get("artist") or info.get("uploader") or info.get("channel"),
        "thumbnail": _thumb(info),
        "webpage_url": url,
        "downloadable": True,
    }
    if info.get("_type") == "playlist" or "entries" in info:
        items = []
        for i, e in enumerate(info.get("entries") or [], 1):
            if not e or not e.get("url"):
                continue
            reason = _entry_unavailable(e)
            items.append({"index": i, "id": e.get("id"), "url": e["url"], "title": _entry_title(e),
                          "uploader": e.get("uploader") or e.get("channel"), "duration": e.get("duration"),
                          "thumbnail": e.get("thumbnail") or _thumb(e), "available": reason is None,
                          "unavailable_reason": reason})
        total = info.get("playlist_count") or len(items)
        available = sum(1 for it in items if it["available"])
        result.update(kind="playlist", count=total, item_count=total, entries=items, truncated=total > len(items),
                      available_count=available, unavailable_count=len(items) - available,
                      has_video=platform == "youtube")
        return result
    if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
        raise MediaError("Live streams are not supported.")
    duration = info.get("duration")
    result.update(kind="single", id=info.get("id"), duration=duration, item_count=1,
                  too_long=bool(duration and duration > settings.max_video_duration),
                  **_format_summary(info))
    return result


# ---------------------------------------------------------------- Spotify (metadata only)

_SPOTIFY_PATH = re.compile(r"^/(?:intl-[a-z]{2}(?:-[a-z]{2})?/)?(track|album|playlist)/([A-Za-z0-9]{22})(?:/|$)")


def _http_get(url: str, timeout: float = 15, max_bytes: int = 3_000_000) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})  # noqa: S310 - fixed https host
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https host
        return resp.read(max_bytes).decode("utf-8", errors="replace")


def spotify_metadata(url: str) -> dict:
    """Read public metadata from Spotify's embed page. Never touches audio streams."""
    match = _SPOTIFY_PATH.match(urlsplit(url).path)
    if not match:
        raise MediaError("Only Spotify track, album and playlist URLs are supported.")
    kind, item_id = match.groups()
    try:
        html = _http_get(f"https://open.spotify.com/embed/{kind}/{item_id}")
        data = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.S)
        entity = json.loads(data.group(1))["props"]["pageProps"]["state"]["data"]["entity"]
    except Exception as exc:  # network error, layout change, removed item
        log.warning("spotify metadata failed url=%s err=%r", url, exc)
        raise MediaError("Could not read Spotify metadata (item unavailable or private).") from None
    images = sorted((entity.get("visualIdentity") or {}).get("image") or [], key=lambda i: i.get("maxWidth") or 0)
    artists = ", ".join(a.get("name", "") for a in entity.get("artists") or []) or entity.get("subtitle")
    tracks = entity.get("trackList") or ([{"title": entity.get("title") or entity.get("name"), "subtitle": artists,
                                           "duration": entity.get("duration")}] if kind == "track" else [])
    entries = [{"index": i, "title": t.get("title"), "uploader": (t.get("subtitle") or "").replace("\xa0", " "),
                "duration": round(t["duration"] / 1000) if t.get("duration") else None}
               for i, t in enumerate(tracks, 1)]
    return {
        "platform": "spotify",
        "kind": "spotify",
        "media_type": kind,
        "spotify_type": kind,
        "title": entity.get("title") or entity.get("name"),
        "uploader": artists,
        "album": entity.get("title") if kind == "album" else None,
        "thumbnail": images[-1]["url"] if images else None,
        "webpage_url": f"https://open.spotify.com/{kind}/{item_id}",
        "count": len(entries),
        "item_count": len(entries),
        "entries": entries,
        "downloadable": False,
        "notice": "Spotify audio is DRM-protected and is never downloaded. Use 'Find source' to look for a "
                  "matching public upload on SoundCloud or YouTube; the file will come from that source.",
    }


def search_candidates(query: str, limit: int = 4) -> list[dict]:
    """Find possible public sources for a track (used by the Spotify resolver)."""
    results = []
    for source, prefix in (("soundcloud", "scsearch"), ("youtube", "ytsearch")):
        try:
            info = _ytdlp_json(f"{prefix}{limit}:{query}", ["--flat-playlist"], settings.analyze_timeout)
        except MediaError as exc:
            log.info("search %s failed: %s", source, exc)
            continue
        for e in info.get("entries") or []:
            if not e or not e.get("url"):
                continue
            link = e.get("webpage_url") or e["url"]
            if source == "soundcloud" and "api.soundcloud.com" in link:
                link = e.get("webpage_url") or ""
            if source == "youtube" and not link.startswith("http"):
                link = f"https://www.youtube.com/watch?v={e.get('id')}"
            if link:
                results.append({"source": source, "url": link, "title": e.get("title") or _entry_title(e),
                                "uploader": e.get("uploader") or e.get("channel"), "duration": e.get("duration")})
    return results


# ---------------------------------------------------------------- download

def format_args(mode: str, fmt: str, quality: str) -> list[str]:
    if mode == "audio":
        if fmt not in AUDIO_FORMATS:
            raise MediaError("Unsupported audio format.")
        args = ["-f", "ba/b", "-x", "--embed-metadata", "--embed-thumbnail", "--convert-thumbnails", "jpg"]
        if fmt != "original":
            args += ["--audio-format", fmt, "--audio-quality", "0"]
        return args
    if mode != "video" or fmt not in VIDEO_FORMATS or quality not in VIDEO_QUALITIES:
        raise MediaError("Unsupported video format or quality.")
    if fmt == "webm":
        sort = ["-S", f"res:{quality}"] if quality != "best" else []
        return ["-f", "bv*[ext=webm]+ba[ext=webm]/b[ext=webm]", *sort, "--merge-output-format", "webm",
                "--embed-metadata"]
    if quality == "best":
        return ["-f", "bv*+ba/b", "--merge-output-format", "mp4/mkv", "--embed-metadata"]
    return ["-f", "bv*+ba/b", "-S", f"res:{quality},vcodec:h264,acodec:m4a", "--merge-output-format", "mp4",
            "--remux-video", "mp4", "--embed-metadata"]


def output_name(info: dict, mode: str, number: int | None, width: int = 2) -> str:
    title = info.get("title") or "Untitled"
    artist = info.get("artist") or info.get("uploader") or info.get("channel") or ""
    if mode == "video":
        base = f"{title} [{info.get('id')}]" if info.get("id") else title
    elif artist and not title.lower().startswith(artist.lower()):
        base = f"{artist} - {title}"
    else:
        base = title
    if number is not None:
        base = f"{number:0{width}d} - {base}"
    return sanitize_filename(base, info.get("ext") or "")


_PROGRESS = re.compile(r"^PROG (\S+) (\S+) (\S+)$")
_POSTPROC = re.compile(r"^POST (\S+) (\S+)$")


def download(url: str, mode: str, fmt: str, quality: str, out_dir: Path, slot: int, timeout: float,
             on_progress: Callable[[str, float | None], None], register=None,
             track: int | None = None, album: str | None = None) -> dict:
    """Download one item into out_dir. Returns info dict incl. 'filepath'."""
    out_dir.mkdir(parents=True, exist_ok=True)
    meta: list[str] = []
    if track is not None:
        meta += ["-metadata", f"track={track}"]
    if album:
        meta += ["-metadata", f"album={album[:200]}"]
    args = base_args() + format_args(mode, fmt, quality) + [
        "--no-playlist", "--playlist-items", "1", "--max-filesize", f"{settings.max_download_size_mb}M",
        "--match-filters", f"!is_live & duration <=? {settings.max_video_duration}",
        "--paths", str(out_dir), "--paths", f"temp:{out_dir}", "-o", f"{slot:03d}.%(ext)s",
        "--newline", "--progress",
        "--progress-template", "download:PROG %(progress.downloaded_bytes)s %(progress.total_bytes)s %(progress.total_bytes_estimate)s",
        "--progress-template", "postprocess:POST %(progress.postprocessor)s %(progress.status)s",
        "--print", "before_dl:INFO %(.{id,title,artist,uploader,channel})j",
        "--print", "after_move:FILE %(filepath)j",
    ]
    if meta:
        args += ["--postprocessor-args", "Metadata:" + shlex.join(meta)]
    args += ["--", url]
    info: dict = {}

    def on_line(line: str) -> None:
        if m := _PROGRESS.match(line):
            done, total, estimate = m.groups()
            total_n = total if total != "NA" else estimate
            try:
                on_progress("downloading", min(float(done) / float(total_n), 1.0))
            except (ValueError, ZeroDivisionError):
                on_progress("downloading", None)
        elif m := _POSTPROC.match(line):
            on_progress("merging" if m.group(1) == "Merger" else "processing", None)
        elif line.startswith("INFO "):
            info.update(json.loads(line[5:]))
        elif line.startswith("FILE "):
            info["filepath"] = json.loads(line[5:])

    code, out, err = run_ytdlp(args, timeout, on_line, register)
    path = Path(info.get("filepath") or "")
    if code != 0 and info and not path.is_file() and "EmbedThumbnail" in out and "Unable to embed" in err:
        # Cover-art embedding is cosmetic: keep the already converted file instead of failing the item.
        produced = [p for p in out_dir.glob(f"{slot:03d}.*") if p.suffix not in (".jpg", ".webp", ".png", ".part", ".ytdl")]
        if len(produced) == 1:
            log.info("thumbnail embed failed, keeping file without cover url=%s", url)
            path, code = produced[0], 0
    if code != 0 or not info or not path.is_file() or out_dir.resolve() not in path.resolve().parents:
        combined = err + out
        log.warning("yt-dlp download failed url=%s code=%s stderr=%s", url, code, combined[-3000:])
        raise MediaError(friendly_error(combined))
    for leftover in out_dir.glob(f"{slot:03d}.*"):
        if leftover != path:
            leftover.unlink(missing_ok=True)
    info["filepath"] = str(path)
    info["ext"] = path.suffix.lstrip(".")
    return info
