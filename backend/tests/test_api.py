from unittest import mock

import pytest
from fastapi.testclient import TestClient

from app import main


@pytest.fixture()
def client():
    main.limiter.__init__()
    with TestClient(main.app) as c:
        yield c


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "max_playlist_items" in r.json()["limits"]


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/api/health", "https://example.com/a.mp4", "file:///etc/passwd"])
def test_analyze_rejects_bad_urls(client, url):
    r = client.post("/api/analyze", json={"url": url})
    assert r.status_code == 400
    assert "error" in r.json() and "Traceback" not in r.text


def test_job_rejects_unknown_format_and_extra_args(client):
    with mock.patch("app.main.validate_url", return_value=("https://www.youtube.com/watch?v=x", "youtube")):
        r = client.post("/api/jobs", json={"url": "https://www.youtube.com/watch?v=x", "mode": "audio", "format": "--exec"})
    assert r.status_code == 400


def test_job_playlist_limit(client):
    urls = [f"https://www.youtube.com/watch?v={i}" for i in range(main.settings.max_playlist_items + 1)]
    with mock.patch("app.main.validate_url", side_effect=lambda u: (u, "youtube")):
        r = client.post("/api/jobs", json={"url": urls[0], "mode": "audio", "format": "mp3", "items": urls})
    assert r.status_code == 400
    assert "items per job" in r.json()["error"]


def test_spotify_cannot_be_downloaded(client):
    with mock.patch("app.main.validate_url", return_value=("https://open.spotify.com/track/x", "spotify")):
        r = client.post("/api/jobs", json={"url": "https://open.spotify.com/track/x", "mode": "audio", "format": "mp3"})
    assert r.status_code == 400


def test_body_size_limit(client):
    r = client.post("/api/analyze", content=b'{"url":"' + b"a" * 40000 + b'"}', headers={"content-type": "application/json"})
    assert r.status_code == 413


def test_unknown_job_and_traversal(client):
    assert client.get("/api/jobs/doesnotexist0000").status_code == 404
    assert client.get("/api/jobs/..%2F..%2Fetc/files/0").status_code == 404
    assert client.get("/api/jobs/abc/files/0").status_code == 404


def test_rate_limit(client, monkeypatch):
    monkeypatch.setattr(main.settings.__class__, "rate_limit_per_minute", property(lambda s: 2), raising=False)
    codes = [client.post("/api/analyze", json={"url": "https://evil.com"}).status_code for _ in range(3)]
    assert codes == [400, 400, 429]
