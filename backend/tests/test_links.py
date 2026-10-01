from unittest import mock

import pytest

from app import media, security
from app.security import InvalidURL, canonicalize, normalize_input, resolve_short_link, strip_tracking, validate_url

SHORT = "https://on.soundcloud.com/7Zj8Us1gsTJhqOWXvl"
PUBLIC_DNS = [(2, 1, 6, "", ("18.66.1.1", 443))]


@pytest.mark.parametrize("raw,expected", [
    (f"  {SHORT}  ", SHORT),
    (f"Listen to playlist:\n{SHORT}\nthanks", SHORT),
    (f"abc {SHORT} xyz", SHORT),
    (f'"{SHORT}"', SHORT),
    (f"<{SHORT}>.", SHORT),
    (f"({SHORT}),", SHORT),
    ("https://www.youtube.com/watch?v=abc&list=PL123.", "https://www.youtube.com/watch?v=abc&list=PL123"),
    ("https://en.wikipedia.org/wiki/Foo_(bar)", "https://en.wikipedia.org/wiki/Foo_(bar)"),
    ("not a url", "not a url"),
])
def test_normalize_input(raw, expected):
    assert normalize_input(raw) == expected


def test_strip_tracking_only_on_soundcloud():
    assert strip_tracking("https://soundcloud.com/a/sets/b?ref=clipboard&p=a&c=0&si=x&utm_source=y") == "https://soundcloud.com/a/sets/b"
    yt = "https://www.youtube.com/watch?v=abc&list=PL123&si=z"
    assert strip_tracking(yt) == yt


def test_api_v2_only_track_paths():
    assert validate_url("https://api-v2.soundcloud.com/tracks/2361058496", resolve=False)[1] == "soundcloud"
    for bad in ["https://api-v2.soundcloud.com/me", "https://api-v2.soundcloud.com/tracks/1/../../me",
                "https://api-v2.soundcloud.com/resolve?url=http://127.0.0.1"]:
        with pytest.raises(InvalidURL):
            validate_url(bad, resolve=False)


def _hops(*responses):
    return mock.patch.object(security, "_head_pinned", side_effect=list(responses))


def test_short_link_resolves_to_canonical():
    loc = "https://soundcloud.com/t-o-767798772/sets/zang-remix?ref=clipboard&p=a&c=0&si=46&utm_source=clipboard"
    with _hops((302, loc)), mock.patch("socket.getaddrinfo", return_value=PUBLIC_DNS):
        assert resolve_short_link(SHORT) == "https://soundcloud.com/t-o-767798772/sets/zang-remix"


@pytest.mark.parametrize("location", [
    "http://127.0.0.1/admin", "http://169.254.169.254/latest/meta-data", "http://[::1]/", "https://evil.example/",
    "file:///etc/passwd", "gopher://soundcloud.com/", "https://soundcloud.com:8443/x", "https://user:pw@soundcloud.com/x",
])
def test_short_link_redirect_targets_are_revalidated(location):
    with _hops((302, location)), mock.patch("socket.getaddrinfo", return_value=PUBLIC_DNS), pytest.raises(InvalidURL):
        resolve_short_link(SHORT)


def test_short_link_redirect_to_allowed_host_resolving_private_is_blocked():
    private = [(2, 1, 6, "", ("10.0.0.5", 443))]
    with _hops((302, "https://soundcloud.com/a/b")), mock.patch("socket.getaddrinfo", return_value=private), pytest.raises(InvalidURL):
        resolve_short_link(SHORT)


def test_short_link_loop_and_errors():
    with _hops(*[(302, SHORT)] * 10), mock.patch("socket.getaddrinfo", return_value=PUBLIC_DNS), pytest.raises(InvalidURL, match="too many"):
        resolve_short_link(SHORT)
    with _hops((404, None)), pytest.raises(InvalidURL, match="expired"):
        resolve_short_link(SHORT)
    with _hops(OSError("timed out")), pytest.raises(InvalidURL, match="Could not resolve SoundCloud shared link"):
        resolve_short_link(SHORT)


def test_pinned_request_refuses_private_dns():
    with mock.patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))]), pytest.raises(InvalidURL):
        security._head_pinned(SHORT)


def test_canonicalize_handles_text_and_short_links():
    with mock.patch.object(security, "resolve_short_link", return_value="https://soundcloud.com/a/sets/b") as r, \
            mock.patch("socket.getaddrinfo", return_value=PUBLIC_DNS):
        assert canonicalize(f"abc {SHORT} xyz") == ("https://soundcloud.com/a/sets/b", "soundcloud")
        r.assert_called_once_with(SHORT)


@pytest.mark.parametrize("platform,info,url,expected", [
    ("youtube", {}, "https://www.youtube.com/watch?v=x", "video"),
    ("youtube", {}, "https://www.youtube.com/shorts/x", "shorts"),
    ("youtube", {"_type": "playlist"}, "https://www.youtube.com/playlist?list=PL1", "playlist"),
    ("youtube", {"_type": "playlist"}, "https://www.youtube.com/@BlenderStudio", "channel"),
    ("soundcloud", {}, "https://soundcloud.com/a/b", "track"),
    ("soundcloud", {"_type": "playlist", "extractor": "soundcloud:set"}, "https://soundcloud.com/a/sets/b", "playlist"),
    ("soundcloud", {"_type": "playlist", "extractor": "soundcloud:set", "album_type": "album"}, "https://soundcloud.com/a/sets/b", "album"),
    ("soundcloud", {"_type": "playlist", "extractor": "soundcloud:user"}, "https://soundcloud.com/a", "profile"),
])
def test_media_type(platform, info, url, expected):
    assert media.media_type(platform, info, url) == expected


def test_unavailable_entries_are_flagged_not_fatal():
    assert media._entry_unavailable({"sc_policy": "ALLOW", "duration": 100}) is None
    assert "Go+" in media._entry_unavailable({"sc_policy": "SNIP"})
    assert "region" in media._entry_unavailable({"sc_policy": "BLOCK"})
    assert media._entry_unavailable({"title": "[Private video]"})
    assert media._entry_unavailable({"duration": 10**6})


@pytest.mark.network
def test_live_short_link():
    assert canonicalize(SHORT) == ("https://soundcloud.com/t-o-767798772/sets/zang-remix", "soundcloud")
