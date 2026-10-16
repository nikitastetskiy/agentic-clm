"""Render hashicorp.com pages that sit behind the Vercel Security Checkpoint.

The events page and the blog listing pages answer plain HTTP clients with a 429 JavaScript challenge.
A headless Chromium runs the challenge and receives the clearance cookie, so those pages are rendered
with Playwright. One browser session serves every page of a run (the cookie carries over).

From a datacenter network — GitHub's hosted runners, where the weekly build runs — the checkpoint
also turns away a browser that reports itself as automated (``navigator.webdriver``), while it lets
the same browser through from an office or home network. Tested on a runner on 2026-10-16: every
setup that reported automation was turned away, every one that did not cleared in about 3 seconds.
So the session does not report it: Chromium starts without its automation flag and with its own user
agent minus "Headless". The repository owner chose this on 2026-10-16 for a build that reads a handful
of public pages a week (docs/pipelines.md) — for hashicorp.com's own checkpoint (OWN_SITE), and for
nothing else: a page on another host (the registration pages on events.hashicorp.com, which answer an
automated visitor with a CAPTCHA) is opened with ``session(report_automation=True)``, a browser that
says what it is, and a CAPTCHA there is never worked around.

Playwright is imported lazily: callers catch ImportError to degrade when it is not installed.
"""
from __future__ import annotations

from contextlib import contextmanager
from urllib.parse import urlparse

LAUNCH_ARGS = ("--disable-blink-features=AutomationControlled",)
CHECKPOINT_TITLES = ("Security Checkpoint", "Just a moment")
OWN_SITE = ("www.hashicorp.com", "hashicorp.com")   # where the session does not report automation


def own_site(url: str) -> bool:
    return (urlparse(url).hostname or "").lower() in OWN_SITE


class CheckpointError(RuntimeError):
    """The bot check never cleared — an environment problem (blocked network / IP), not a parser bug."""


def user_agent(raw: str) -> str:
    """The browser's own user agent without the "Headless" marker."""
    return raw.replace("HeadlessChrome/", "Chrome/")


@contextmanager
def session(*, report_automation: bool = False):
    """A browser context for hashicorp.com pages; with `report_automation`, one for any other site."""
    from playwright.sync_api import sync_playwright  # lazy so the fallback paths need no dependency

    with sync_playwright() as p:
        # channel="chromium" is the full browser in its current headless mode, not the separate
        # headless shell Playwright uses by default.
        browser = p.chromium.launch(headless=True, channel="chromium",
                                    args=[] if report_automation else list(LAUNCH_ARGS))
        try:
            probe = browser.new_page()
            agent = probe.evaluate("navigator.userAgent")
            agent = agent if report_automation else user_agent(agent)
            probe.close()
            yield browser.new_context(user_agent=agent, viewport={"width": 1280, "height": 2000},
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
