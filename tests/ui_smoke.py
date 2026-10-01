"""Browser smoke test for the frontend (desktop + Android viewport).

Usage: python tests/ui_smoke.py <frontend_url> [screenshot_dir]
Requires: pip install playwright && playwright install chromium
"""

import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

SOUNDCLOUD = "https://soundcloud.com/unwritten-stories/overthinking-creative-commons-free-happy-electronic-music"
PLAYLIST = "https://soundcloud.com/the-concept-band/sets/the-royal-concept-ep"  # listing only, nothing downloaded
SPOTIFY = "https://open.spotify.com/track/4uLU6hMCjMI75M1A2tKUQC"


def analyze(page, url):
    page.fill("#url", url)
    page.click("#analyze")
    expect(page.locator("#result")).to_be_visible(timeout=120_000)


def run(base: str, shots: Path | None) -> None:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for name, opts in {"desktop": {"viewport": {"width": 1280, "height": 900}},
                           "android": {**p.devices["Pixel 7"]}}.items():
            ctx = browser.new_context(**opts, accept_downloads=True)
            page = ctx.new_page()
            errors = []
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.goto(base, wait_until="networkidle")
            expect(page.locator("h1")).to_have_text("Universal Media Downloader")
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "horizontal scroll"
            if shots:
                page.screenshot(path=shots / f"{name}-home.png")

            # Single item, audio, end to end
            # YouTube is often bot-checked from cloud IPs, so the live test downloads the CC SoundCloud track.
            analyze(page, SOUNDCLOUD)
            page.select_option("#format", "mp3")
            page.click("#download")
            job = page.locator(".job").first
            expect(job.locator(".pill")).to_have_text("Ready", timeout=300_000)
            with page.expect_download(timeout=60_000) as dl:
                job.locator(".job-files a").first.click()
            print(f"[{name}] single download ok: {dl.value.suggested_filename}")
            if shots:
                page.screenshot(path=shots / f"{name}-single.png", full_page=True)

            # Playlist listing + selection limit message
            analyze(page, PLAYLIST)
            expect(page.locator("#playlist")).to_be_visible()
            count = page.locator("#entries input[type=checkbox]").count()
            page.uncheck("#select-all")
            expect(page.locator("#selected-count")).to_have_text("Selected: 0")
            page.check("#select-all")
            if count > 25:
                expect(page.locator("#limit-warning")).to_be_visible()
                assert page.locator("#download").is_disabled()
            print(f"[{name}] playlist listed {count} items, limit warning ok")
            if shots:
                page.screenshot(path=shots / f"{name}-playlist.png")

            # Spotify: metadata only, no download button
            analyze(page, SPOTIFY)
            expect(page.locator("#notice")).to_contain_text("DRM")
            assert not page.locator("#download").is_visible()
            print(f"[{name}] spotify metadata ok")

            # Friendly error, no traceback
            page.fill("#url", "http://127.0.0.1/admin")
            page.click("#analyze")
            expect(page.locator("#error")).to_contain_text("Unsupported URL")
            assert not errors, errors
            ctx.close()
        browser.close()


if __name__ == "__main__":
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    if out:
        out.mkdir(parents=True, exist_ok=True)
    run(sys.argv[1], out)
    print("UI SMOKE PASS")
