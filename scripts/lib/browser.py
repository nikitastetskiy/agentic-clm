"""Render hashicorp.com pages that sit behind the Vercel Security Checkpoint.

The events page and the blog listing pages answer plain HTTP clients with a 429 JavaScript challenge.
A headless Chromium runs the challenge and receives the clearance cookie, so those pages are rendered
with Playwright. One browser session serves every page of a run (the cookie carries over).

Playwright is imported lazily: callers catch ImportError to degrade when it is not installed.
"""
from __future__ import annotations

from contextlib import contextmanager

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
CHECKPOINT_TITLES = ("Security Checkpoint", "Just a moment")


class CheckpointError(RuntimeError):
    """The bot check never cleared — an environment problem (blocked network / IP), not a parser bug."""


@contextmanager
def session():
    from playwright.sync_api import sync_playwright  # lazy so the fallback paths need no dependency

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            yield browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 2000},
                                      locale="en-US")
        finally:
            browser.close()


def open_page(ctx, url: str, *, timeout_ms: int = 50000, scroll: bool = True):
    """Load `url`, wait out the checkpoint, optionally scroll to the bottom so every lazy-rendered
    card loads, and return the Playwright page (the caller closes it)."""
    page = ctx.new_page()
    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
    # The checkpoint page auto-reloads into the real page once the JS challenge clears.
    for _ in range(10):
        page.wait_for_timeout(2500)
        if not any(t in page.title() for t in CHECKPOINT_TITLES):
            break
    else:
        page.close()
        raise CheckpointError(f"security checkpoint did not clear for {url}")
    page.wait_for_timeout(2000)
    if scroll:
        prev = 0
        for _ in range(40):
            height = page.evaluate("document.body.scrollHeight")
            page.evaluate(f"window.scrollTo(0,{height})")
            page.wait_for_timeout(500)
            if height <= prev:
                break
            prev = height
        page.evaluate("window.scrollTo(0,0)")
        page.wait_for_timeout(300)
    return page
