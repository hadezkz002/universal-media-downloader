import importlib
from unittest import mock

import pytest

from app import media
from app.security import InvalidURL, RateLimiter, is_public_ip, sanitize_filename, validate_url


@pytest.mark.parametrize("url,platform", [
    ("https://www.youtube.com/watch?v=jNQXAC9IVRw", "youtube"),
    ("https://youtu.be/jNQXAC9IVRw", "youtube"),
    ("https://youtube.com/shorts/abc", "youtube"),
    ("https://m.youtube.com/playlist?list=PL1", "youtube"),
    ("https://music.youtube.com/watch?v=x", "youtube"),
    ("http://soundcloud.com/a/b", "soundcloud"),
    ("https://on.soundcloud.com/xyz", "soundcloud"),
    ("https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC", "spotify"),
    ("https://WWW.YouTube.com./watch?v=x", "youtube"),
])
def test_platform_detection(url, platform):
    normalized, detected = validate_url(url, resolve=False)
    assert detected == platform
    assert normalized.startswith("https://")


@pytest.mark.parametrize("url", [
    "", "   ", "ftp://youtube.com/x", "file:///etc/passwd", "javascript:alert(1)",
    "https://evil.com/watch?v=x", "https://youtube.com.evil.com/x", "https://evilyoutube.com/x",
    "http://127.0.0.1/", "http://localhost/", "http://[::1]/", "http://169.254.169.254/latest/meta-data",
    "http://metadata.google.internal/", "http://10.0.0.1/", "http://192.168.1.1/",
    "https://user:pass@youtube.com/x", "https://youtube.com:8080/x", "https://youtube.com:99999/x",
    "https://youtube.com/watch?v=x y", "https://youtube.com/\nx", "https://" + "a" * 3000 + ".com",
])
def test_rejected_urls(url):
    with pytest.raises(InvalidURL):
        validate_url(url, resolve=False)


def test_dns_resolving_to_private_ip_is_blocked():
    fake = [(2, 1, 6, "", ("10.1.2.3", 443))]
    with mock.patch("socket.getaddrinfo", return_value=fake), pytest.raises(InvalidURL):
        validate_url("https://www.youtube.com/watch?v=x")
    public = [(2, 1, 6, "", ("142.250.1.1", 443))]
    with mock.patch("socket.getaddrinfo", return_value=public):
        assert validate_url("https://www.youtube.com/watch?v=x")[1] == "youtube"


@pytest.mark.parametrize("ip,public", [
    ("8.8.8.8", True), ("2606:4700:4700::1111", True),
    ("127.0.0.1", False), ("127.8.9.1", False), ("10.0.0.1", False), ("172.16.5.4", False),
    ("192.168.0.1", False), ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False),
    ("::1", False), ("fe80::1", False), ("fe80::1%eth0", False), ("fc00::1", False), ("fd12::1", False),
    ("::ffff:127.0.0.1", False), ("::ffff:10.0.0.1", False), ("224.0.0.1", False), ("ff02::1", False),
])
def test_is_public_ip(ip, public):
    assert is_public_ip(ip) is public


@pytest.mark.parametrize("name", [
    "Sơn Tùng M-TP - Chúng Ta Của Hiện Tại", "周杰伦 - 晴天", "米津玄師 - Lemon", "🔥 Fire 🎵 Mix 👨‍👩‍👧",
])
def test_sanitize_keeps_unicode(name):
    assert sanitize_filename(name, "mp3") == name + ".mp3"


def test_sanitize_strips_dangerous():
    assert sanitize_filename("../../etc/passwd", "mp3") == "_.._etc_passwd.mp3"
    assert "/" not in sanitize_filename("a/b\\c:d*e?f\"g<h>i|j\x00k\x1f", "m4a")
    assert sanitize_filename("", "mp3") == "download.mp3"
    assert sanitize_filename("...", "") == "download"
    assert sanitize_filename("CON", "mp3") == "_CON.mp3"
    assert sanitize_filename("a‮b", "mp3") == "ab.mp3"  # RTL override removed
    assert sanitize_filename("x", "m/p..3") == "x.mp3"


def test_sanitize_truncates_on_utf8_boundary():
    out = sanitize_filename("Tiếng Việt 日本語 🎵" * 40, "flac")
    assert len(out.encode()) <= 180
    assert out.endswith(".flac")
    out.encode("utf-8").decode("utf-8")


def test_output_names():
    info = {"title": "Lemon", "artist": "米津玄師", "id": "abc", "ext": "mp3"}
    assert media.output_name(info, "audio", None) == "米津玄師 - Lemon.mp3"
    assert media.output_name(info, "audio", 3) == "03 - 米津玄師 - Lemon.mp3"
    assert media.output_name({**info, "ext": "mp4"}, "video", None) == "Lemon [abc].mp4"
    assert media.output_name({"title": "Rick Astley - Never", "uploader": "Rick Astley", "ext": "m4a"}, "audio", None) == "Rick Astley - Never.m4a"
    assert media.output_name(info, "audio", 7, width=3) == "007 - 米津玄師 - Lemon.mp3"


def test_format_args_only_enumerated_values():
    assert "--audio-format" in media.format_args("audio", "mp3", "best")
    assert "--audio-format" not in media.format_args("audio", "original", "best")
    assert "res:720,vcodec:h264,acodec:m4a" in media.format_args("video", "mp4", "720")
    for bad in [("audio", "--exec", "best"), ("video", "mp4", "--exec rm"), ("video", "avi", "best"), ("shell", "mp3", "best")]:
        with pytest.raises(media.MediaError):
            media.format_args(*bad)


def test_friendly_errors():
    assert "bot check" in media.friendly_error("ERROR: Sign in to confirm you're not a bot")
    assert "authentication" in media.friendly_error("ERROR: Private video. Sign in")
    assert "DRM" in media.friendly_error("ERROR: This video is DRM protected")
    assert media.friendly_error("Traceback (most recent call last): boom") == "Media unavailable or could not be processed."


def test_rate_limiter():
    rl = RateLimiter()
    assert all(rl.allow("1.2.3.4", "b", 3, 60) for _ in range(3))
    assert not rl.allow("1.2.3.4", "b", 3, 60)
    assert rl.allow("5.6.7.8", "b", 3, 60)
    assert rl.allow("1.2.3.4", "other", 3, 60)


def test_config_from_env(monkeypatch):
    from app import config
    monkeypatch.setenv("MAX_PLAYLIST_ITEMS", "7")
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://a.example, https://b.example")
    s = importlib.reload(config).Settings()
    assert s.max_playlist_items == 7
    assert s.allowed_origins == ["https://a.example", "https://b.example"]
    monkeypatch.setenv("MAX_PLAYLIST_ITEMS", "-1")
    with pytest.raises(ValueError):
        config.Settings()
