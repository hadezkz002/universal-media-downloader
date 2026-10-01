"""Browser smoke test for the frontend (desktop + Android viewport).

Usage: python tests/ui_smoke.py <frontend_url> [screenshot_dir]
Requires: pip install playwright && playwright install chromium
"""

import sys
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

SOUNDCLOUD = "https://soundcloud.com/unwritten-stories/overthinking-creative-commons-free-happy-electronic-music"
PLAYLIST = "https://soundcloud.com/the-concept-band/sets/the-royal-concept-ep"  # listing only, has Go+ (unavailable) items
SHORT_PLAYLIST = "https://on.soundcloud.com/7Zj8Us1gsTJhqOWXvl"  # share link to a public set
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

            # Paste share text with a short link: no Analyze click; detection + resolve + playlist mode.
            page.fill("#url", f"abc {SHORT_PLAYLIST} xyz")
            expect(page.locator("#detect")).to_contain_text("SoundCloud")
            expect(page.locator("#detect")).to_contain_text("Playlist detected", timeout=120_000)
            expect(page.locator("#playlist")).to_be_visible()
            count = page.locator("#entries input[type=checkbox]").count()
            assert count > 1, count
            if count > 25:  # pre-selected up to the server limit, "Select all" shows a partial state
                expect(page.locator("#selected-count")).to_have_text("Selected: 25")
                assert page.evaluate("document.getElementById('select-all').indeterminate")
            page.check("#select-all")
            if count > 25:
                expect(page.locator("#limit-warning")).to_be_visible()
                assert page.locator("#download").is_disabled()
            page.uncheck("#select-all")
            expect(page.locator("#selected-count")).to_have_text("Selected: 0")
            page.locator("#entries input[type=checkbox]").first.check()
            page.click("#download")
            job = page.locator(".job").first
            expect(job.locator(".pill")).to_have_text("Ready", timeout=300_000)
            expect(job.locator(".job-files a")).to_have_count(1)
            with page.expect_download(timeout=60_000) as dl:
                job.locator(".job-files a").first.click()
            print(f"[{name}] short-link playlist: {count} items listed, downloaded {dl.value.suggested_filename}")
            if shots:
                page.screenshot(path=shots / f"{name}-playlist.png")
            job.locator(".remove").click()

            # Canonical set with Go+ items: unavailable rows are disabled, not fatal.
            page.fill("#url", PLAYLIST)
            expect(page.locator("#detect")).to_contain_text("Playlist detected", timeout=120_000)
            expect(page.locator("#availability")).to_contain_text("Unavailable")
            assert page.locator("#entries input[type=checkbox]:disabled").count() >= 1
            print(f"[{name}] unavailable items flagged")

            # Spotify: metadata only, no download button
            analyze(page, SPOTIFY)
            expect(page.locator("#notice")).to_contain_text("DRM")
            assert not page.locator("#download").is_visible()
            print(f"[{name}] spotify metadata ok")

            # Friendly errors, no traceback
            page.fill("#url", "http://127.0.0.1/admin")
            expect(page.locator("#detect")).to_contain_text("Unsupported site")
            page.fill("#url", "https://on.soundcloud.com/doesnotexist-zzzz")
            expect(page.locator("#error")).to_contain_text("invalid or has expired", timeout=60_000)
            assert not errors, errors
            ctx.close()
        browser.close()


if __name__ == "__main__":
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    if out:
        out.mkdir(parents=True, exist_ok=True)
    run(sys.argv[1], out)
    print("UI SMOKE PASS")
