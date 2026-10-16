#!/usr/bin/env python3
"""Fetch upcoming HashiCorp events from the events page's own data, and render the page for the rest.

The events page (https://www.hashicorp.com/en/events) fills its event cards from a search index
(Algolia), which it queries from the browser with a public, search-only key. The same query, made
directly, returns the same records (date, type, title, link, product badges) as structured data and
without the page's **Vercel Security Checkpoint** — a JavaScript challenge that answers plain HTTP
clients with 429. The index is therefore the source of the dated events (``dated_from``).

The page itself is still rendered with a headless Chromium (lib/browser.py) for what the index does
not hold — the undated "Featured Events" promotions and each event's abstract — and its cards are
compared with the index: an event only the page shows is kept, and when the index does not answer
the page's cards are the source, as they were before the index was used. Then `data/events.json`:

  * ``webinars`` — events whose type is one of ``events.slot_types`` (WEBINAR, WORKSHOP), bucketed by
    product. These fill the slots of the product "Upcoming Webinars" slides, each labelled with its
    own type. A session badged for several products (Vault + Terraform) is listed under each.
  * ``featured`` — every other type (conference, summit, …): in-person events with a slide of their
    own, plus the hand-maintained events in ``events.manual`` (config/featured-events.yaml) that are
    not on the public page.

Product tagging is **badge-driven**: each card shows one coloured product icon per product
(``class="flight-icon-<product>-color"`` with a ``<span class="sr-only">Terraform</span>`` label),
so an event is bucketed by the product(s) it is actually badged with. The title text is NEVER
keyword-matched to a product (that over-tagged, e.g. a "policy"/"security" title onto Vault). Card
text is line-structured:
    JUN 24, 2026|
    WEBINAR
    Hands-on workshop: Vault Radar
    Vault
=> date / type / title (the trailing product line is the badge's screen-reader label).

The page's "Featured Events" box promotes events that carry NO date there (an executive roundtable,
a webinar whose date is only on its registration page, a city series whose next sessions are "Coming
soon"). They cannot go on a slide without a date, but they must not vanish either: they are written to
``unscheduled``, each marked online or in person (series pages are opened for their upcoming city
sessions, in the region scope), and the weekly summary lists them, so a person can add a date in
config/featured-events.yaml.

Online vs in person: webinars and workshops are ONLINE sessions (product webinar slides; never
region-filtered — the page's region tag on them is the audience time zone). Every other type is an
IN-PERSON event (the featured section), and only those are held to ``events.scope_regions``.

Each event's own page is then opened for its abstract (``abstract``, with ``abstract_status``). That
is best-effort: registration pages on events.hashicorp.com sit behind a CAPTCHA, which is never
worked around — those events simply carry no abstract and the deck uses a curated or generic line.

If the page cannot be rendered (Playwright missing, or the bot check does not clear) the dated events
still come from the index; the week then has no abstracts and no promotions, which ``note`` says. Only
when neither source answers is `data/events.json` written empty with ``status: "unavailable"`` (the
event slides are left out of the deck — never faked), exit 0. A page that renders but yields no event
card while the index did not answer is treated as a layout change and exits 1.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import browser  # noqa: E402
from lib.common import REPO_ROOT, load_config, log, today as run_date, write_output  # noqa: E402
from lib.regions import (in_scope, place_in, resolve_scope_regions, scope_events,  # noqa: E402
                         unknown_regions)

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

EVENTS_URL = "https://www.hashicorp.com/en/events"
BASE = "https://www.hashicorp.com"
INDEX_QUERY_URL = "https://{app}-dsn.algolia.net/1/indexes/*/queries"
WEBINAR_URL = BASE + "/events/webinars/{slug}"   # where the page links a webinar record
DEFAULT_SLOT_TYPES = ("WEBINAR",)
# An event page's abstract is its first substantial block of prose that is not form/legal boilerplate.
ABSTRACT_MIN_BLOCK = 90
ABSTRACT_MAX_CHARS = 900
BOILERPLATE_RE = re.compile(
    r"privacy|consent|unsubscribe|opt-out|cookie|rights reserved|terms of (use|service)|"
    r"being part of a community|by submitting|book a strategy session", re.I)
PRODUCTS = ["Terraform", "Vault", "Boundary", "Consul", "Nomad", "Vagrant", "Packer", "Waypoint"]
MONTHS = {m.upper(): i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
DATE_RE = re.compile(r"^([A-Z]{3})\s+(\d{1,2}),\s+(\d{4})")
SESSION_WHEN_RE = re.compile(r"^(\d{1,2} (January|February|March|April|May|June|July|August|September|"
                             r"October|November|December)|Coming soon)$", re.I)
RECORDING_CTA = {"watch now", "watch on demand", "view recording"}


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


CARDS_JS = """() => {
  const seen = new Set(); const out = [];
  document.querySelectorAll('a[href]').forEach(a => {
    const card = a.closest('li,article,div'); if (!card) return;
    const text = (card.innerText || '').trim();
    if (!/^[A-Z]{3}\\s+\\d{1,2},\\s+\\d{4}/.test(text)) return;  // event cards start with a date
    const key = text.slice(0, 80);
    if (seen.has(key)) return; seen.add(key);
    // Product tags come from the card's BADGES, not the title text: each product badge has a
    // screen-reader label (<span class="sr-only">Terraform</span>) and a coloured product icon
    // (class "flight-icon-<product>-color"). This is the authoritative product signal.
    const products = new Set();
    card.querySelectorAll('[class*="badge"] .sr-only').forEach(s => {
      const t = (s.textContent || '').trim().toLowerCase(); if (t) products.add(t);
    });
    card.querySelectorAll('[class*="flight-icon-"]').forEach(el => {
      (el.getAttribute('class') || '').split(/\\s+/).forEach(c => {
        const m = c.match(/^flight-icon-([a-z0-9]+)-color$/); if (m) products.add(m[1]);
      });
    });
    out.push({ href: a.getAttribute('href') || '', text, products: [...products] });
  });
  return out;
}"""


PROMOS_JS = """() => {
  // Undated promotions (the "Featured Events" box): a card with a call-to-action link and no date line.
  const seen = new Set(); const out = [];
  document.querySelectorAll('a[href]').forEach(a => {
    const cta = (a.innerText || '').trim();
    if (!/^(register( here)?|reserve your seat|sign up|learn more|watch now)$/i.test(cta)) return;
    let card = a;
    for (let i = 0; i < 6 && card.parentElement; i++) {
      card = card.parentElement;
      if ((card.innerText || '').split('\\n').filter(l => l.trim()).length > 3) break;
    }
    const text = (card.innerText || '').trim();
    if (/^[A-Z]{3}\\s+\\d{1,2},\\s+\\d{4}/.test(text) || seen.has(text)) return;
    seen.add(text);
    out.push({ href: (a.getAttribute('href') || '').trim(), cta, text });
  });
  return out;
}"""


HAS_CARD_JS = """() => [...document.querySelectorAll('a[href]')].some(a => {
  const card = a.closest('li,article,div');
  return card && /^[A-Z]{3}\\s+\\d{1,2},\\s+\\d{4}/.test((card.innerText || '').trim());
})"""


def render_cards(ctx, attempts: int = 2) -> tuple[list[dict], list[dict]]:
    """Render the events page (passing the Vercel checkpoint): (dated event cards, undated promotions),
    both raw. The dated list is filled in by the page's own script, sometimes seconds after the rest
    (one run in a few read it empty), so wait for a first card, and load the page once more if none
    came: a slow page must not fail the weekly build."""
    for attempt in range(1, attempts + 1):
        page = browser.open_page(ctx, EVENTS_URL)
        try:
            try:
                page.wait_for_function(HAS_CARD_JS, timeout=20000)
            except Exception:  # noqa: BLE001 — read what is there; an empty read is handled below
                pass
            cards, promos = page.evaluate(CARDS_JS), page.evaluate(PROMOS_JS)
        finally:
            page.close()
        if cards or attempt == attempts:
            return cards, promos
        log(f"events page showed no dated event yet (attempt {attempt}); loading it again")
    return [], []


def absolute(href: str) -> str:
    return href if href.startswith("http") else BASE + ("" if href.startswith("/") else "/") + href


def parse_promo(card: dict) -> dict | None:
    """An undated promotion card -> {type, title, products, url}; None for a recording or a non-event."""
    lines = [ln.strip() for ln in card["text"].splitlines() if ln.strip()]
    if len(lines) < 3 or card["cta"].lower() in RECORDING_CTA or "on-demand" in lines[0].lower():
        return None
    return {"type": lines[0].upper(), "title": lines[1],
            "products": sorted({ln.lower() for ln in lines if ln in PRODUCTS}), "url": absolute(card["href"])}


def parse_series_sessions(lines: list[str]) -> list[dict]:
    """Upcoming sessions on a series landing page: a place followed by its date ("10 November") or
    "Coming soon". Everything after "Past Events" is history."""
    lines = [ln.strip() for ln in lines if ln.strip()]
    upto = next((i for i, ln in enumerate(lines) if ln.lower() == "past events"), len(lines))
    return [{"place": place, "when": when} for place, when in zip(lines[:upto], lines[1:upto])
            if SESSION_WHEN_RE.match(when) and len(place) < 40 and not SESSION_WHEN_RE.match(place)]


def series_sessions(ctx, url: str) -> list[dict]:
    page = browser.open_page(ctx, url, timeout_ms=40000)
    try:
        return parse_series_sessions(page.evaluate("() => (document.querySelector('main') || "
                                                   "document.body).innerText").split("\n"))
    finally:
        page.close()


def unscheduled_events(promos: list[dict], events: dict, scope: list[str], sessions_of) -> list[dict]:
    """Promoted events that are not on any slide: no date on the events page, or a series session
    that is "Coming soon" or not yet featured. Each says its ``format``: ``online`` (a webinar or
    workshop — it would be a card), ``in person`` (a city session — it would be a featured slide, so
    the region scope applies), or ``format not stated``. `sessions_of(url)` reads a series page."""
    shown = [e for bucket in events["webinars"].values() for e in bucket] + events["featured"]
    known = [f"{e['title']} {e.get('location', '')}".lower() for e in shown]
    urls = {e.get("url") for e in shown} | {lk["url"] for e in shown for lk in e.get("links") or []}
    out = []
    for promo in promos:
        if promo["url"] in urls or any(promo["title"].lower() in k for k in known):
            continue
        sessions = sessions_of(promo["url"]) if "/lp/" in promo["url"] else []
        for s in sessions:
            if any(s["place"].lower() in k for k in known):
                continue                     # already featured (e.g. from config/featured-events.yaml)
            item = {**promo, "title": f"{promo['title']} — {s['place']}", "location": s["place"],
                    "when": s["when"], "format": "in person"}
            if in_scope(item, scope):
                out.append(item)
        if not sessions:
            online = re.search(r"\b(WEBINAR|WORKSHOP)\b", promo["type"])
            out.append({**promo, "location": "", "when": "no date on the events page",
                        "format": "online" if online else "format not stated"})
    return out


def pick_abstract(blocks: list[str]) -> str:
    """The event abstract from a page's text blocks: the first substantial block of prose that is
    not boilerplate, plus the blocks that follow it while they still read as the same abstract."""
    prose = [" ".join(b.split()) for b in blocks]
    prose = [b for b in prose if len(b) >= ABSTRACT_MIN_BLOCK and not BOILERPLATE_RE.search(b)
             and not b.startswith("**")]
    out = ""
    for block in prose[:3]:
        if out and len(out) + len(block) > ABSTRACT_MAX_CHARS:
            break
        out = f"{out} {block}".strip()
    return out


def fetch_abstract(ctx, url: str) -> tuple[str, str]:
    """(abstract, status) for an event's own page. Status is ``ok``, ``blocked`` (CAPTCHA — never
    worked around), ``empty`` or ``error``."""
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=40000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001 — chat widgets keep some pages busy; the DOM is still usable
            pass
        page.wait_for_timeout(1500)
        if any("captcha" in f.url.lower() for f in page.frames):
            return "", "blocked"
        if any(t in page.title() for t in browser.CHECKPOINT_TITLES):
            return "", "blocked"
        text = page.evaluate("() => document.body ? document.body.innerText : ''")
        abstract = pick_abstract(text.split("\n"))
        return (abstract, "ok") if abstract else ("", "empty")
    except Exception as exc:  # noqa: BLE001
        log(f"   abstract fetch failed for {url}: {str(exc).splitlines()[0][:120]}")
        return "", "error"
    finally:
        page.close()


def event_pages(events: dict, wanted) -> dict[str, list[dict]]:
    """url -> the copies of the event it belongs to, for the kept events whose url `wanted(url)` accepts
    (a hand-maintained event carries its own description and is skipped)."""
    by_url: dict[str, list[dict]] = {}
    for ev in [e for bucket in events["webinars"].values() for e in bucket] + events["featured"]:
        if ev.get("source") != "manual" and wanted(ev["url"]):
            by_url.setdefault(ev["url"], []).append(ev)
    return by_url


def attach_abstracts(ctx, events: dict, wanted=lambda url: True) -> None:
    """Open each kept event's page once (those `wanted` accepts) and record its abstract on every copy
    of the event."""
    for url, copies in event_pages(events, wanted).items():
        abstract, status = fetch_abstract(ctx, url)
        log(f"   abstract [{status}] {copies[0]['title'][:60]}")
        for ev in copies:
            ev["abstract"], ev["abstract_status"] = abstract, status


def parse_card(card: dict, today: str) -> dict | None:
    """Turn a raw {href, text, products} card into a structured event (upcoming only), or None to drop.

    Products come from the card's BADGES (``card['products']`` — the per-product icon/sr-only label),
    which is authoritative. We fall back to innerText lines that exactly equal a product name only when
    the card carried no badge at all (defensive), and never keyword-guess from the title."""
    lines = [ln.strip() for ln in card["text"].splitlines() if ln.strip()]
    if not lines:
        return None
    iso = parse_date(lines[0])
    if not iso or iso < today:  # past or unparseable -> drop
        return None
    etype = (lines[1] if len(lines) > 1 else "").upper().strip(" |")
    known = {p.lower() for p in PRODUCTS}
    products = [p for p in (card.get("products") or []) if p in known]
    if not products:  # no badge captured -> fall back to a product NAME on its own innerText line
        products = [ln.lower() for ln in lines if ln in PRODUCTS]
    products = sorted(set(products))
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
        "products": products,
        "url": url,
    }


def index_hits(icfg: dict, since: int, *, attempts: int = 2) -> list[dict]:
    """The events page's search-index records that start after `since` (epoch seconds): the query the
    page itself makes, with the public search-only key it ships to every visitor."""
    body = json.dumps({"requests": [{"indexName": icfg["name"], "filters": f"startTimestamp > {since}",
                                     "hitsPerPage": 200}]}).encode()
    req = urllib.request.Request(INDEX_QUERY_URL.format(app=icfg["app_id"].lower()), data=body, headers={
        "X-Algolia-Application-Id": icfg["app_id"], "X-Algolia-API-Key": icfg["search_key"],
        "Content-Type": "application/json", "User-Agent": "auto-pptx weekly build"})
    for attempt in range(1, attempts + 1):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)["results"][0]["hits"]
        except (urllib.error.URLError, TimeoutError, KeyError, IndexError, ValueError) as exc:
            if attempt == attempts:
                raise
            log(f"events index did not answer ({str(exc)[:120]}); asking again")
            time.sleep(3)
    return []


def event_url(hit: dict) -> str:
    """Where the events page links an index record: its registration page when it has one (the
    workshops on events.hashicorp.com), a webinar's own page, otherwise the events page."""
    kind = hit.get("eventType") or {}
    if kind.get("url"):
        return kind["url"]
    if kind.get("__typename") == "EventWebinarRecord" and hit.get("slug"):
        return WEBINAR_URL.format(slug=hit["slug"])
    return EVENTS_URL


def parse_hit(hit: dict, today: str) -> dict | None:
    """An index record -> the structured event parse_card makes of a rendered card; None for a past,
    hidden or recorded one. The date is the event's own local date, as the page prints it."""
    if hit.get("isHidden") or (hit.get("eventType") or {}).get("hidden") or hit.get("recorded"):
        return None
    try:
        start = datetime.fromisoformat(hit.get("startTime") or "")
    except ValueError:
        return None
    iso, title = start.date().isoformat(), " ".join((hit.get("title") or "").split())
    if iso < today or not title:
        return None
    products = {(b.get("slug") or "").lower() for b in hit.get("badges") or []}
    products |= {str(p).lower() for p in hit.get("products") or []}
    d = datetime.fromisoformat(iso)
    return {
        "date": iso,
        "display_date": d.strftime("%b %-d, %Y"),
        "display_date_short": d.strftime("%B %-d"),
        "title": title,
        "type": (hit.get("type") or "EVENT").upper(),
        "products": sorted(products & {p.lower() for p in PRODUCTS}),
        "url": event_url(hit),
    }


def event_key(e: dict) -> tuple[str, str]:
    return e["date"], " ".join(e["title"].split()).lower()


def reconcile(indexed: list[dict] | None, rendered: list[dict]) -> tuple[list[dict], list[str]]:
    """The dated events for the deck, and where the two sources differ. The index is the source when
    it answered; an event only the rendered page shows is kept too, and the page's link replaces the
    generic one an index record without a link of its own gets. Without the index, the page's cards."""
    if indexed is None:
        return rendered, []
    page = {event_key(e): e for e in rendered}
    keys = {event_key(e) for e in indexed}
    out = [{**e, "url": page[event_key(e)]["url"]} if e["url"] == EVENTS_URL and event_key(e) in page else e
           for e in indexed]
    out += [e for k, e in page.items() if k not in keys]
    differ = ([f"only in the index: {d} {t}" for d, t in sorted(keys - set(page))] if rendered else [])
    differ += [f"only on the page: {d} {t}" for d, t in sorted(set(page) - keys)]
    return sorted(out, key=lambda e: e["date"]), differ


def _slot(e: dict) -> dict:
    return {"date": e["date"], "display_date": e["display_date"], "title": e["title"],
            "url": e["url"], "description": "", "products": e["products"], "type": e["type"],
            "abstract": "", "abstract_status": "not_fetched"}


def _featured(e: dict) -> dict:
    # Location: prefer a trailing place after a dash ("... Exchange - London"); otherwise fall back to
    # any known place named in the title ("AWS Summit Zurich" -> Zurich, "CISO UK" -> UK) so the
    # city-photo resolver always has something to match, even without the deck-data agent.
    loc = ""
    m = re.search(r"[-\u2013]\s*([A-Z][\w .'/]+)$", e["title"])
    if m:
        loc = m.group(1).strip()
    if not loc:
        loc = place_in(e["title"])
    return {"date": e["date"], "display_date": e["display_date_short"], "title": e["title"],
            "location": loc, "type": e["type"].title(), "url": e["url"],
            "links": [{"label": "Register Here", "url": e["url"]}],
            "products": e["products"], "description": "", "language": "en",
            "abstract": "", "abstract_status": "not_fetched"}


def parse_cards(cards: list[dict], today: str) -> list[dict]:
    """The upcoming events among the rendered page's raw cards, by date."""
    return sorted((e for e in (parse_card(c, today) for c in cards) if e), key=lambda e: e["date"])


def build_events(cards: list[dict], today: str, webinar_cfg: dict,
                 slot_types: tuple[str, ...] = DEFAULT_SLOT_TYPES) -> dict:
    return bucket_events(parse_cards(cards, today), today, webinar_cfg, slot_types)


def bucket_events(events: list[dict], today: str, webinar_cfg: dict,
                  slot_types: tuple[str, ...] = DEFAULT_SLOT_TYPES) -> dict:
    """Structured events (from the index or the page) -> the product webinar slots and the featured
    (in-person) events."""
    events = sorted(events, key=lambda e: e["date"])
    # Webinars are bucketed by their PRODUCT BADGE (authoritative). A webinar tagged with several
    # products (e.g. Boundary + Vault) is placed in each bucket it belongs to. "other" keeps the FULL
    # cross-product pool — sessions whose badge isn't a deck product (or that carry no badge) — which
    # the event editor uses to BACKFILL leftover webinar slots. We do NOT keyword-guess from the title
    # (that used to over-tag, e.g. a "policy"/"security" title onto Vault); the only title fallback is a
    # product NAME appearing as its own word, used solely when a webinar has no product badge at all.
    buckets = [p for p in webinar_cfg.keys() if p in ("terraform", "vault")]
    webinars = {"terraform": [], "vault": [], "other": []}
    featured = []
    for e in events:
        if e["type"] in slot_types:
            placed = False
            for prod in buckets:
                if prod in e["products"]:
                    webinars[prod].append(_slot(e))
                    placed = True
            if not placed and not e["products"]:  # badge-less webinar -> last-resort product-name match
                low = e["title"].lower()
                for prod in buckets:
                    if re.search(rf"\b{re.escape(prod)}\b", low):
                        webinars[prod].append(_slot(e))
                        placed = True
            if not placed:  # tagged for a non-deck product, or unmatched -> cross-product pool
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


def load_manual_events(path: Path, today: str) -> list[dict]:
    """Upcoming hand-maintained events (config/featured-events.yaml) in the featured-event shape.
    Past-dated entries are skipped, so the file never needs pruning on a deadline."""
    if yaml is None or not path.exists():
        return []
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    out = []
    for ev in raw.get("events") or []:
        iso = str(ev.get("date", ""))[:10]
        if not ev.get("title") or not iso or iso < today:
            continue
        links = [{"label": str(lk["label"]), "url": str(lk["url"])} for lk in ev.get("links") or []
                 if lk.get("label") and lk.get("url")]
        if not links and ev.get("url"):
            links = [{"label": "Register Here", "url": str(ev["url"])}]
        d = datetime.fromisoformat(iso)
        out.append({
            "date": iso, "display_date": d.strftime("%B %-d"), "title": str(ev["title"]),
            "location": str(ev.get("location", "")), "type": str(ev.get("type", "Event")),
            "url": links[0]["url"] if links else "", "links": links,
            "products": [str(p).lower() for p in ev.get("products") or []],
            "description": str(ev.get("description", "")),
            "description_points": [str(p) for p in ev.get("description_points") or []],
            "language": str(ev.get("language", "en")), "visuals": str(ev.get("visuals", "")),
            "abstract": "", "abstract_status": "manual", "source": "manual",
        })
    return out


def scope_in_person(events: dict, scope: list[str]) -> dict:
    """Apply the region scope to the IN-PERSON events only. Webinars and workshops are online: anyone
    can join, and the page's own "region" tag on them is the audience time zone (all of them read
    "Americas" on 2026-10-14, scheduled 08:00 Pacific), not a venue — so they are never dropped."""
    in_person = {"featured": events["featured"], "webinars": {}}
    report = scope_events(in_person, scope)
    events["featured"] = in_person["featured"]
    return report


def merge_manual(events: dict, manual: list[dict], slot_types: tuple[str, ...]) -> None:
    """Hand-maintained events join the fetched ones: a webinar or workshop as a card on each product
    slide it is badged for, anything else as a featured slide of its own. An event the public page
    lists after all (same link) is not added twice."""
    listed = {e["url"] for bucket in events["webinars"].values() for e in bucket}
    listed |= {e["url"] for e in events["featured"]}
    for ev in manual:
        if ev["url"] in listed:
            continue
        if ev["type"].upper() in slot_types:
            d = datetime.fromisoformat(ev["date"])
            card = {**ev, "type": ev["type"].upper(), "display_date": d.strftime("%b %-d, %Y"),
                    "description": ev["description"] or " ".join(ev["description_points"])}
            for product in ("terraform", "vault"):
                if product in ev["products"]:
                    events["webinars"][product].append(dict(card))
        else:
            events["featured"].append(ev)
    for bucket in events["webinars"].values():
        bucket.sort(key=lambda e: e["date"])
    events["featured"].sort(key=lambda e: e["date"])


def unavailable(note: str) -> dict:
    return {"status": "unavailable", "note": note, "source": EVENTS_URL,
            "webinars": {"terraform": [], "vault": [], "other": []}, "featured": []}


def assemble(listed: list[dict], today: str, webinar_cfg: dict, slot_types: tuple[str, ...],
             manual: list[dict], scope: list[str]) -> dict:
    """The dated events, the hand-maintained ones and the region scope -> the content of events.json."""
    events = bucket_events(listed, today, webinar_cfg, slot_types)
    merge_manual(events, manual, slot_types)
    rep = scope_in_person(events, scope)
    if scope:
        log(f"region scope {scope} (in-person events; online sessions are open to all): "
            f"kept {rep['kept']}, dropped {len(rep['dropped'])} out-of-region")
        for d in rep["dropped"]:
            log(f"   - dropped [{d['region']}] {d['bucket']}: {d['title']}")
    else:
        log("region scope: GLOBAL (no region filter)")
    return events


def main() -> int:
    cfg = load_config()
    ecfg = cfg["events"]
    today = run_date().isoformat()
    slot_types = tuple(t.upper() for t in ecfg.get("slot_types") or DEFAULT_SLOT_TYPES)
    manual = load_manual_events(REPO_ROOT / ecfg["manual"], today) if ecfg.get("manual") else []
    scope = resolve_scope_regions(ecfg.get("scope_regions"))
    bad = unknown_regions(scope)
    if bad:
        log(f"WARNING: unrecognized scope region(s) {bad}; known: EMEA, MENA, APAC, AMERICAS (or GLOBAL)")

    indexed = None
    if ecfg.get("index"):
        since = int(datetime.fromisoformat(today).replace(tzinfo=timezone.utc).timestamp()) - 86400
        try:
            indexed = sorted((e for e in (parse_hit(h, today) for h in index_hits(ecfg["index"], since)) if e),
                             key=lambda e: e["date"])
            log(f"events index: {len(indexed)} upcoming event(s)")
        except Exception as exc:  # noqa: BLE001 — the rendered page is the other source
            log(f"events index unavailable ({str(exc).splitlines()[0][:160]}); the rendered page is the only source")

    def without_page(note: str) -> dict:
        if indexed is None:
            log(f"events unavailable ({note})")
            events = unavailable(note)
            merge_manual(events, manual, slot_types)
            return events
        events = assemble(indexed, today, ecfg["webinars"], slot_types, manual, scope)
        events.update(dated_from="index", unscheduled=[],
                      note=f"events page not rendered ({note}); the dated events come from its index, "
                           "without abstracts or the undated promotions")
        return events

    try:
        with browser.session() as ctx:
            cards, promos = render_cards(ctx)
            rendered = parse_cards(cards, today)
            log(f"events page rendered; {len(cards)} candidate cards ({len(rendered)} upcoming), "
                f"{len(promos)} undated promotions")
            if indexed is None and not rendered:
                # The page rendered, no card parsed and the index did not answer: almost certainly a
                # layout change, which is a code problem — fail loudly rather than ship a deck that
                # silently lost its events.
                log("ERROR: rendered events page yielded zero upcoming events (layout change?)")
                write_output("events.json", unavailable("events page rendered but no event card parsed"))
                return 1
            listed, differ = reconcile(indexed, rendered)
            for line in differ:
                log(f"   events page and index differ — {line}")
            events = assemble(listed, today, ecfg["webinars"], slot_types, manual, scope)
            events["dated_from"] = "page" if indexed is None else "index"
            attach_abstracts(ctx, events, browser.own_site)
            events["unscheduled"] = []
            try:
                events["unscheduled"] = unscheduled_events(
                    [p for p in (parse_promo(c) for c in promos) if p], events, scope,
                    lambda url: series_sessions(ctx, url))
            except Exception as exc:  # noqa: BLE001 — a report-only extra must not cost the slides
                log(f"WARNING: undated promotions not read ({str(exc).splitlines()[0][:120]})")
            for item in events["unscheduled"]:
                log(f"   not on a slide ({item['format']}, {item['when']}): {item['type'].title()} — {item['title']}")
        elsewhere = lambda url: not browser.own_site(url)  # noqa: E731
        if event_pages(events, elsewhere):
            # Event pages on other sites (registration pages on events.hashicorp.com) are opened by a
            # browser that says it is automated: a CAPTCHA there is never worked around.
            try:
                with browser.session(report_automation=True) as plain:
                    attach_abstracts(plain, events, elsewhere)
            except Exception as exc:  # noqa: BLE001 — abstracts are best-effort
                log(f"WARNING: event pages on other sites not read ({str(exc).splitlines()[0][:120]})")
    except ImportError:
        log("playwright not installed (run `python -m playwright install chromium`)")
        events = without_page("playwright unavailable")
    except Exception as exc:  # noqa: BLE001 — a blocked/failed render is an environment problem
        log(f"events render failed ({str(exc).splitlines()[0][:160]})")
        events = without_page(f"render failed: {type(exc).__name__}")

    events["counts"] = {"terraform_webinars": len(events["webinars"]["terraform"]),
                        "vault_webinars": len(events["webinars"]["vault"]),
                        "other_webinars": len(events["webinars"]["other"]),
                        "featured": len(events["featured"])}
    write_output("events.json", events)
    c = events["counts"]
    log(f"events.json written [{events['status']}]: terraform={c['terraform_webinars']} "
        f"vault={c['vault_webinars']} other={c['other_webinars']} featured={c['featured']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
