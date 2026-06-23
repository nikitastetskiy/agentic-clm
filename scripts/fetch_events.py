#!/usr/bin/env python3
"""Fetch upcoming HashiCorp events by RENDERING the events page with Playwright.

The events page (https://www.hashicorp.com/en/events) sits behind a **Vercel Security
Checkpoint** (a JavaScript challenge) that returns HTTP 429 to plain HTTP clients AND to the
agent's `web_fetch` tool. A headless Chromium passes the challenge — it executes the challenge
JS and receives the clearance cookie — so this script renders the page, extracts the event
cards (date, type, title, url, product tags) straight from the live DOM, buckets WEBINARs by
product and collects CONFERENCE/in-person events as `featured`, and writes `data/events.json`
with `status: "ok"`. This is the deterministic replacement for the old flaky agent web_fetch.

Card text on the page is line-structured, e.g.:
    JUN 24, 2026|
    WEBINAR
    Hands-on workshop: Vault Radar
    Vault
=> date / type / title / product-tags. Webinars are bucketed by their product tags (a far more
reliable signal than keyword-matching the title).

If Playwright or its browser is unavailable, or rendering fails, this falls back to the legacy
behavior: write `data/events.request.json` (so the deck-data agent can attempt web_fetch) and an
empty `data/events.json`. Empty/unavailable event slides are then HIDDEN downstream — never faked.
"""
from __future__ import annotations

import re
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.common import DATA_DIR, load_config, log, write_output  # noqa: E402
from lib.regions import resolve_scope_regions, scope_events, unknown_regions  # noqa: E402

EVENTS_URL = "https://www.hashicorp.com/en/events"
BASE = "https://www.hashicorp.com"
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
PRODUCTS = ["Terraform", "Vault", "Boundary", "Consul", "Nomad", "Vagrant", "Packer", "Waypoint"]
MONTHS = {m.upper(): i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
DATE_RE = re.compile(r"^([A-Z]{3})\s+(\d{1,2}),\s+(\d{4})")


def parse_date(token: str) -> str | None:
    """'JUN 24, 2026' -> '2026-06-24' (ISO), or None."""
    m = DATE_RE.match(token.strip())
    if not m:
        return None
    mon, day, year = m.group(1), int(m.group(2)), int(m.group(3))
    if mon not in MONTHS:
        return None
    try:
        return date(year, MONTHS[mon], day).isoformat()
    except ValueError:
        return None


def render_cards(timeout_ms: int = 50000) -> list[dict]:
    """Render the events page (passing the Vercel checkpoint) and return raw {href, text} cards."""
    from playwright.sync_api import sync_playwright  # lazy import so the fallback path needs no dep

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 2000}, locale="en-US")
        page = ctx.new_page()
        page.goto(EVENTS_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        # The checkpoint page auto-reloads into the real page once the JS challenge clears.
        for _ in range(8):
            page.wait_for_timeout(2500)
            if "Security Checkpoint" not in page.title() and "Just a moment" not in page.title():
                break
        page.wait_for_timeout(2500)
        # Scroll to the bottom in steps until the page height stops growing, so EVERY lazy-rendered
        # event card loads (not just the first screenful) — captures the full upcoming list.
        prev_h = 0
        for _ in range(40):
            h = page.evaluate("document.body.scrollHeight")
            page.evaluate(f"window.scrollTo(0,{h})")
            page.wait_for_timeout(500)
            if h <= prev_h:
                break
            prev_h = h
        page.evaluate("window.scrollTo(0,0)")
        page.wait_for_timeout(300)
        cards = page.evaluate(
            """() => {
              const seen = new Set(); const out = [];
              document.querySelectorAll('a[href]').forEach(a => {
                const card = a.closest('li,article,div'); if (!card) return;
                const text = (card.innerText || '').trim();
                if (!/^[A-Z]{3}\\s+\\d{1,2},\\s+\\d{4}/.test(text)) return;  // event cards start with a date
                const key = text.slice(0, 80);
                if (seen.has(key)) return; seen.add(key);
                out.push({ href: a.getAttribute('href') || '', text });
              });
              return out;
            }"""
        )
        browser.close()
        return cards


def parse_card(card: dict, today: str) -> dict | None:
    """Turn a raw {href, text} card into a structured event (upcoming only), or None to drop it."""
    lines = [ln.strip() for ln in card["text"].splitlines() if ln.strip()]
    if not lines:
        return None
    iso = parse_date(lines[0])
    if not iso or iso < today:  # past or unparseable -> drop
        return None
    etype = (lines[1] if len(lines) > 1 else "").upper().strip(" |")
    products = [ln for ln in lines if ln in PRODUCTS]
    title = next((ln for ln in lines[2:] if ln not in PRODUCTS and "|" not in ln), "").strip()
    if not title:
        return None
    href = card["href"]
    url = href if href.startswith("http") else BASE + ("" if href.startswith("/") else "/") + href
    d = datetime.fromisoformat(iso)
    return {
        "date": iso,
        "display_date": d.strftime("%b %-d, %Y"),
        "display_date_short": d.strftime("%B %-d"),
        "title": title,
        "type": etype or "EVENT",
        "products": [p.lower() for p in products],
        "url": url,
    }


def _slot(e: dict) -> dict:
    return {"date": e["date"], "display_date": e["display_date"], "title": e["title"],
            "url": e["url"], "description": "", "products": e["products"], "type": e["type"]}


def _featured(e: dict) -> dict:
    # Heuristic location: the trailing place in titles like "... Exchange - London" / "AWS Summit DC".
    loc = ""
    m = re.search(r"[-\u2013]\s*([A-Z][\w .'/]+)$", e["title"])
    if m:
        loc = m.group(1).strip()
    return {"date": e["date"], "display_date": e["display_date_short"], "title": e["title"],
            "location": loc, "type": e["type"].title(), "url": e["url"],
            "products": e["products"], "description": "", "language": "en"}


def build_events(cards: list[dict], today: str, webinar_cfg: dict) -> dict:
    events = sorted(
        (e for e in (parse_card(c, today) for c in cards) if e),
        key=lambda e: e["date"],
    )
    # "other" keeps the FULL webinar pool — untagged/certification/cross-product/regional sessions
    # that don't map to a primary product bucket. The event editor uses it (plus featured) to BACKFILL
    # any leftover empty webinar slots, exactly as the human-authored deck does (it never left a slot
    # blank). Without this pool those webinars were silently dropped and the slot went empty.
    webinars = {"terraform": [], "vault": [], "other": []}
    featured = []
    for e in events:
        if e["type"] == "WEBINAR":
            placed = False
            for prod in ("terraform", "vault"):
                if prod in e["products"]:
                    webinars[prod].append(_slot(e))
                    placed = True
            if not placed:  # untagged webinar -> keyword fallback so it is not lost
                low = e["title"].lower()
                for prod, meta in webinar_cfg.items():
                    if prod in webinars and any(k in low for k in meta.get("keywords", [])):
                        webinars[prod].append(_slot(e))
                        placed = True
            if not placed:  # still unplaced -> keep in the cross-product pool for backfill
                webinars["other"].append(_slot(e))
        else:
            featured.append(_featured(e))
    return {
        "status": "ok",
        "source": EVENTS_URL,
        "today": today,
        "counts": {"terraform_webinars": len(webinars["terraform"]),
                   "vault_webinars": len(webinars["vault"]),
                   "other_webinars": len(webinars["other"]),
                   "featured": len(featured)},
        "webinars": webinars,
        "featured": featured,
    }


def write_request(pages: dict, ecfg: dict, today: str, note: str) -> None:
    """Legacy fallback: ask the deck-data agent to try, and ensure events.json exists (empty)."""
    write_output("events.request.json", {
        "today": today,
        "status": "needs_agent_fetch",
        "note": note,
        "instructions": (
            "Use web_fetch on each page below. Extract UPCOMING events only (date >= today). "
            "Bucket webinars by product keyword; collect featured/in-person events separately. "
            "Write the result to data/events.json using the schema in 'output_schema'."
        ),
        "pages": pages,
        "webinars": ecfg["webinars"],
        "recent_count": ecfg.get("recent_count", 3),
        "output_schema": {
            "webinars": {
                "terraform": [{"date": "YYYY-MM-DD", "display_date": "MMM D, YYYY", "title": "...", "url": "...", "description": "1 short sentence"}],
                "vault": [{"date": "YYYY-MM-DD", "display_date": "MMM D, YYYY", "title": "...", "url": "...", "description": "1 short sentence"}],
            },
            "featured": [
                {"date": "YYYY-MM-DD", "display_date": "Month D", "title": "...", "location": "City", "url": "...",
                 "description": "1-2 sentences",
                 "description_points": ["full sentence ~15-30 words", "full sentence ~15-30 words", "full sentence ~15-30 words"],
                 "language": "en"}
            ],
        },
    })
    if not (DATA_DIR / "events.json").exists():
        write_output("events.json", {"status": "pending_agent_fetch",
                                     "webinars": {"terraform": [], "vault": [], "other": []},
                                     "featured": []})


def main() -> int:
    cfg = load_config()
    ecfg = cfg["events"]
    today = date.today().isoformat()

    try:
        cards = render_cards()
        log(f"events page rendered; {len(cards)} candidate cards")
        events = build_events(cards, today, ecfg["webinars"])
        scope = resolve_scope_regions(ecfg.get("scope_regions"))
        bad = unknown_regions(scope)
        if bad:
            log(f"WARNING: unrecognized scope region(s) {bad}; known: EMEA, MENA, APAC, AMERICAS (or GLOBAL)")
        rep = scope_events(events, scope)
        if scope:
            log(f"region scope {scope}: kept {rep['kept']}, dropped {len(rep['dropped'])} out-of-region")
            for d in rep["dropped"]:
                log(f"   - dropped [{d['region']}] {d['bucket']}: {d['title']}")
        else:
            log("region scope: GLOBAL (no region filter)")
        if not (events["webinars"]["terraform"] or events["webinars"]["vault"] or events["featured"]):
            raise RuntimeError("rendered page yielded zero upcoming events (layout change?)")
        write_output("events.json", events)
        c = events["counts"]
        log(f"events.json written: terraform_webinars={c['terraform_webinars']} "
            f"vault_webinars={c['vault_webinars']} featured={c['featured']}")
        return 0
    except ImportError:
        log("playwright not installed; deferring events to the deck-data agent (run "
            "`python -m playwright install chromium` to enable deterministic events)")
        write_request(ecfg["pages"], ecfg, today, "playwright unavailable; agent web_fetch fallback")
        return 0
    except Exception as exc:  # noqa: BLE001 — render/parse failures are non-fatal (agent fallback)
        log(f"events render failed ({exc}); deferring to deck-data agent")
        write_request(ecfg["pages"], ecfg, today, f"playwright render failed: {exc}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
