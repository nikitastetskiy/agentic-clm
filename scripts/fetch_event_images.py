#!/usr/bin/env python3
"""Capture 680px-wide event visuals for the featured-event slides.

For every featured event in `data/events.json` that has a URL, this renders the page with Playwright,
locates the leadspace element (`.bx--leadspace--gradient`, the gradient overlay the event pages use),
blanks the registration clutter (Register button, breadcrumb, "questions / send an e-mail" block) so
the hero reads as a clean title + brand gradient, and screenshots THAT element at a fixed **680px
width**. PNGs go to `data/event-images/` and a manifest keyed by event URL is written to
`data/event-images.json`.

Best-effort and non-fatal: if Playwright/its browser is missing, a page won't render, or a capture is
mostly registration-page whitespace, the event is recorded with `status != "ok"` and the build either
uses a matching clean series hero or keeps the slide's existing picture.

For event series, an on-brand HashiCorp series hero is also captured from the series landing page (see
`SERIES_FALLBACKS`) and written to `fallbacks` in the manifest. The editor prefers each event's OWN
decluttered per-city capture and uses the series hero only as a FALLBACK for events whose page has no
leadspace (e.g. a hands-on lab), so every slide shows a clean city hero instead of a registration-page
fragment.

Usage:
    python scripts/fetch_event_images.py [--width 680] [--include-webinars]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.common import DATA_DIR, log, write_output  # noqa: E402

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

IMAGES_DIR = DATA_DIR / "event-images"
# Leadspace selectors in priority order (the gradient hero first, then looser fallbacks).
SELECTORS = [".bx--leadspace--gradient", ".bx--leadspace__overlay", ".bx--leadspace"]
DEFAULT_WIDTH = 680

# The IBM registration leadspace bundles a "Register" button, a breadcrumb, and a
# "questions / send an e-mail to …" block into the gradient hero. Those read as a form, not a
# deck visual. This script blanks them with `visibility:hidden` (NOT display:none) so the
# leadspace KEEPS its full height: the hero stays taller than the slide frame, which lets the
# editor's cover-crop preserve the full-width brand gradient instead of cropping its sides.
# Pierces shadow DOM because the page is built from slotted web components.
DECLUTTER_JS = r"""() => {
  function deepAll(sel, root=document, acc=[]){
    root.querySelectorAll(sel).forEach(e=>acc.push(e));
    root.querySelectorAll('*').forEach(el=>{ if(el.shadowRoot) deepAll(sel, el.shadowRoot, acc); });
    return acc;
  }
  const hide = e => { if (e && e.style) e.style.setProperty('visibility','hidden','important'); };
  // Register button + breadcrumb nav, by component/slot.
  deepAll('dds-button,cds-button,bx-btn,.bx--btn,button,[slot="action"],slot[name="action"],'
        + 'a[part="link"],[slot="navigation"],slot[name="navigation"]').forEach(hide);
  // Divider rule above the questions block.
  deepAll('hr,[class*="divider"],[class*="rule"]').forEach(hide);
  // Register label + "questions/e-mail" block, matched on text (small containers only).
  deepAll('*').forEach(e => {
    if (e.children.length > 2) return;
    const t = (e.textContent || '').trim();
    if (/^Register\b/i.test(t) && t.length < 40) hide(e);
    if (/(please send an e-?mail|have any questions|@gpj\.com)/i.test(t) && t.length < 240) hide(e);
  });
  const ls = deepAll('.bx--leadspace--gradient')[0];
  if (!ls) return null;
  const r = ls.getBoundingClientRect();
  return {w: Math.round(r.width), h: Math.round(r.height)};
}"""

# On-brand HashiCorp heroes for event SERIES. The editor prefers these across matching featured slides
# so the deck has one clean presentation motif instead of mixed registration-page screenshots.
SERIES_FALLBACKS = [
    {
        "id": "agentic-runtime-security",
        "match": r"agentic\s+runtime\s+security",
        "url": "https://www.hashicorp.com/en/lp/agentic-runtime-security-exchange",
        "selector": ".style_root___EaFI",
    },
]


def slug(event: dict) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", (event.get("title") or "").lower()).strip("-")[:48]
    h = hashlib.sha1((event.get("url") or event.get("title") or "").encode()).hexdigest()[:8]
    return f"{base or 'event'}-{h}"


def assess_capture_quality(path: Path) -> dict:
    """Reject screenshot regions that are mostly blank/registration form rather than a deck visual.

    The IBM registration pages can expose a `.bx--leadspace--gradient` wrapper that extends into the
    white form area below the hero. That produces slides with inconsistent photos (blank areas, buttons,
    contact e-mails). A large white-pixel ratio is a stable signal for this failure mode.
    """
    try:
        from PIL import Image
        with Image.open(path) as im:
            im = im.convert("RGB")
            im.thumbnail((220, 220))
            pixels = list(im.getdata())
    except Exception as exc:  # noqa: BLE001
        return {"status": "unknown", "reason": f"quality check unavailable: {exc!r}"[:160]}
    if not pixels:
        return {"status": "rejected", "reason": "empty screenshot"}
    total = len(pixels)
    white = sum(1 for r, g, b in pixels if r >= 245 and g >= 245 and b >= 245)
    white_ratio = round(white / total, 3)
    if white_ratio >= 0.35:
        return {"status": "rejected", "white_ratio": white_ratio,
                "reason": "large blank/registration-white area"}
    return {"status": "ok", "white_ratio": white_ratio}


def collect_events(events: dict, include_webinars: bool) -> list[dict]:
    out = [e for e in (events.get("featured") or []) if e.get("url")]
    if include_webinars:
        wb = events.get("webinars") or {}
        for bucket in ("terraform", "vault", "other"):
            out += [e for e in (wb.get(bucket) or []) if e.get("url")]
    # de-dupe by URL, preserving order
    seen, uniq = set(), []
    for e in out:
        if e["url"] not in seen:
            seen.add(e["url"]); uniq.append(e)
    return uniq


def capture_one(page, event: dict, width: int) -> dict:
    url = event["url"]
    rec = {"url": url, "title": event.get("title", ""), "status": "error",
           "file": None, "width": width, "height": None, "selector": None}
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"goto: {exc!r}"[:200]
        return rec
    page.wait_for_timeout(3500)  # let the leadspace hydrate
    try:
        page.evaluate(DECLUTTER_JS)  # blank Register button / breadcrumb / e-mail block (keeps height)
        page.wait_for_timeout(350)
    except Exception:  # noqa: BLE001
        pass  # decluttering is best-effort; fall back to the raw leadspace
    el = None
    for sel in SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.bounding_box(timeout=2500):
                el, rec["selector"] = loc, sel
                break
        except Exception:  # noqa: BLE001
            continue
    if el is None:
        rec["error"] = "leadspace element not found"
        return rec
    out_path = IMAGES_DIR / f"{slug(event)}.png"
    try:
        el.scroll_into_view_if_needed(timeout=3000)
        page.wait_for_timeout(400)
        el.screenshot(path=str(out_path))
        box = el.bounding_box()
        rec.update(status="ok", file=str(out_path.relative_to(DATA_DIR.parent)),
                   height=round(box["height"]) if box else None)
        quality = assess_capture_quality(out_path)
        rec["quality"] = quality
        if quality.get("status") == "rejected":
            rec.update(status="rejected", error=f"capture rejected: {quality.get('reason')}")
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"screenshot: {exc!r}"[:200]
    return rec


def capture_series_hero(page, fb: dict, width: int) -> dict:
    """Capture the on-brand HashiCorp series landing-page hero at the mandated `width`.

    Dismisses the cookie banner and hides fixed/sticky overlays (chat widget, sticky header) so the hero
    is clean. The editor prefers this as the common motif for matching event-series slides.
    """
    url = fb["url"]
    rec = {"id": fb["id"], "match": fb["match"], "url": url, "title": "", "status": "error",
           "file": None, "width": width, "height": None, "selector": fb["selector"]}
    try:
        page.goto(url, wait_until="networkidle", timeout=45000)
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"goto: {exc!r}"[:200]
        return rec
    page.wait_for_timeout(1200)
    for label in ("Accept Required Only", "Accept All"):
        try:
            btn = page.get_by_role("button", name=label)
            if btn.count():
                btn.first.click(timeout=3000)
                break
        except Exception:  # noqa: BLE001
            pass
    page.wait_for_timeout(500)
    try:  # hide fixed/sticky overlays + chat iframes so they don't cover the hero
        page.evaluate("""() => {
          for (const el of document.querySelectorAll('body *')) {
            const cs = getComputedStyle(el);
            if ((cs.position==='fixed'||cs.position==='sticky') && el.offsetHeight>0)
              el.style.setProperty('display','none','important');
          }
          for (const f of document.querySelectorAll('iframe')) f.style.setProperty('display','none','important');
        }""")
    except Exception:  # noqa: BLE001
        pass
    page.wait_for_timeout(300)
    try:
        rec["title"] = (page.title() or "")[:120]
    except Exception:  # noqa: BLE001
        pass
    loc = page.locator(fb["selector"]).first
    try:
        if not (loc.count() and loc.bounding_box(timeout=2500)):
            rec["error"] = "series hero element not found"
            return rec
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"locate: {exc!r}"[:200]
        return rec
    out_path = IMAGES_DIR / f"series-{fb['id']}.png"
    try:
        loc.scroll_into_view_if_needed(timeout=3000)
        page.wait_for_timeout(300)
        loc.screenshot(path=str(out_path))
        box = loc.bounding_box()
        rec.update(status="ok", file=str(out_path.relative_to(DATA_DIR.parent)),
                   height=round(box["height"]) if box else None)
    except Exception as exc:  # noqa: BLE001
        rec["error"] = f"screenshot: {exc!r}"[:200]
    return rec


def run(width: int, include_webinars: bool) -> dict:
    try:
        events = json.load(open(DATA_DIR / "events.json", encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"cannot read data/events.json ({exc}); nothing to capture")
        return {"status": "no_events", "images": {}}
    targets = collect_events(events, include_webinars)
    if not targets:
        log("no featured events with URLs; nothing to capture")
        return {"status": "no_events", "images": {}}

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("playwright not installed; skipping image capture "
            "(run `python -m playwright install chromium` to enable)")
        return {"status": "playwright_unavailable", "images": {}}

    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
    images: dict[str, dict] = {}
    ok = 0
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, viewport={"width": width, "height": 1000},
                                  locale="en-US", device_scale_factor=2)
        page = ctx.new_page()
        for e in targets:
            rec = capture_one(page, e, width)
            images[e["url"]] = rec
            ok += rec["status"] == "ok"
            log(f"  [{rec['status']:>5}] {e.get('title', '')[:54]}"
                + (f" -> {rec['file']}" if rec.get("file") else f" ({rec.get('error', '')[:60]})"))
        # On-brand series fallbacks: capture one only when its series appears among the targets AND at
        # least one matching event lacks an OK per-page capture (so we never fetch it needlessly).
        fallbacks: dict[str, dict] = {}
        for fb in SERIES_FALLBACKS:
            pat = re.compile(fb["match"], re.I)
            matching = [e for e in targets if pat.search(e.get("title", ""))]
            need = [e for e in matching if images.get(e["url"], {}).get("status") != "ok"]
            if matching and need:
                rec = capture_series_hero(page, fb, width)
                fallbacks[fb["id"]] = rec
                log(f"  [{rec['status']:>5}] (series fallback) {fb['id']}"
                    + (f" -> {rec['file']}" if rec.get("file") else f" ({rec.get('error', '')[:60]})"))
        browser.close()
    log(f"captured {ok}/{len(targets)} event images at {width}px wide"
        + (f"; {len(fallbacks)} series fallback(s)" if fallbacks else ""))
    return {"status": "ok" if ok or fallbacks else "empty", "width": width, "captured": ok,
            "total": len(targets), "images": images, "fallbacks": fallbacks}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--width", type=int, default=DEFAULT_WIDTH,
                    help="render/screenshot width in px (mandatory dimension; default 680)")
    ap.add_argument("--include-webinars", action="store_true",
                    help="also capture webinar-slot events (default: featured only)")
    args = ap.parse_args()
    payload = run(args.width, args.include_webinars)
    write_output("event-images.json", payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
