#!/usr/bin/env python3
"""Fetch recent HashiCorp blog posts and bucket them by product (Terraform / Vault).

Sources, merged per product:
  * the global Atom feed — the 20 newest posts across all products (plain HTTP);
  * each product's listing page (``blogs.product_pages``) — the newest posts tagged with that
    product. These pages sit behind the Vercel bot check, so they are rendered with a headless
    browser; if that is unavailable the feed alone is used;
  * state/blogs.json — every post seen on earlier runs (seeded from the reference deck), because
    neither live source reaches back far enough to fill the 8 entry boxes of a blog slide.

A post is filed under a product when its TITLE names that product. A title that names a different
HashiCorp product is never filed under this one (a post about HCP Vault listed on the Terraform page
stays off the Terraform slide). A title naming no product falls back to the listing page it appeared
on, then to the product's keywords matched against title, slug and summary.

Writes data/blogs.json (the ``recent_count`` newest posts per product) and updates state/blogs.json.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import browser, state  # noqa: E402
from lib.common import http_get, load_config, log, write_output  # noqa: E402

ATOM = "{http://www.w3.org/2005/Atom}"
BASE = "https://www.hashicorp.com"
# Product names that can appear in a post title. A title naming one of these other than the slide's
# own product belongs to that other product.
PRODUCT_NAMES = ("terraform", "vault", "boundary", "consul", "nomad", "packer", "vagrant", "waypoint")
MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}
CARD_DATE_RE = re.compile(r"^([A-Z][a-z]{2})[a-z]*\.? (\d{1,2}), (\d{4})")
POST_HREF_RE = re.compile(r"^(?:https://www\.hashicorp\.com)?/(?:[a-z]{2}/)?blog/([a-z0-9][a-z0-9-]+)/?$")

LISTING_JS = """() => [...document.querySelectorAll('a[href*="/blog/"]')]
  .map(a => ({href: a.getAttribute('href') || '', text: (a.innerText || '').trim()}))
  .filter(c => c.text)"""


def parse_feed(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    entries = []
    for e in root.findall(f"{ATOM}entry"):
        title_el = e.find(f"{ATOM}title")
        link_el = e.find(f"{ATOM}link")
        updated_el = e.find(f"{ATOM}updated")
        summary_el = e.find(f"{ATOM}summary")
        title = (title_el.text or "").strip() if title_el is not None else ""
        url = link_el.get("href") if link_el is not None else ""
        updated = (updated_el.text or "").strip() if updated_el is not None else ""
        summary = (summary_el.text or "").strip() if summary_el is not None else ""
        if not title or not url:
            continue
        try:
            dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        except ValueError:
            continue
        entries.append({"title": title, "url": url, "date": dt.date().isoformat(), "listed_on": [],
                        "source": "feed", "summary": re.sub(r"<[^>]+>", " ", summary).lower()})
    return entries


def parse_listing(cards: list[dict], product: str) -> list[dict]:
    """Posts from a product listing page's cards. A card's text reads, line by line:
    ``TECHNOLOGY & SOLUTIONS`` / ``<title>`` / ``Sep 28, 2026 |`` / ``5 min read``."""
    posts, seen = [], set()
    for card in cards:
        m = POST_HREF_RE.match(card["href"])
        if not m or m.group(1) in seen:
            continue
        lines = [ln.strip() for ln in card["text"].splitlines() if ln.strip()]
        date_idx = next((i for i, ln in enumerate(lines) if CARD_DATE_RE.match(ln)), None)
        if not date_idx:  # None, or 0 = no title line above the date
            continue
        mon, day, year = CARD_DATE_RE.match(lines[date_idx]).groups()
        if mon not in MONTHS:
            continue
        seen.add(m.group(1))
        posts.append({"title": lines[date_idx - 1], "url": f"{BASE}/en/blog/{m.group(1)}",
                      "date": f"{int(year):04d}-{MONTHS[mon]:02d}-{int(day):02d}",
                      "listed_on": [product], "source": "product-page", "summary": ""})
    return posts


def fetch_listings(pages: dict[str, str]) -> dict[str, list[dict]]:
    """product -> posts on its listing page. Best-effort: {} for a product whose page fails."""
    out: dict[str, list[dict]] = {}
    try:
        with browser.session() as ctx:
            for product, url in pages.items():
                try:
                    page = browser.open_page(ctx, url)
                    out[product] = parse_listing(page.evaluate(LISTING_JS), product)
                    page.close()
                    log(f"  {product} listing page: {len(out[product])} posts")
                except Exception as exc:  # noqa: BLE001
                    log(f"  {product} listing page unavailable: {str(exc).splitlines()[0][:140]}")
    except ImportError:
        log("  playwright not installed; using the feed only")
    except Exception as exc:  # noqa: BLE001
        log(f"  browser session failed ({str(exc).splitlines()[0][:140]}); using the feed only")
    return out


def names_in(title: str) -> set[str]:
    low = title.lower()
    return {name for name in PRODUCT_NAMES if re.search(rf"\b{name}\b", low)}


def belongs(post: dict, product: str, fcfg: dict) -> bool:
    named = names_in(post["title"])
    own = {n.lower() for n in fcfg.get("names") or [product]}
    if named & own:
        return True
    if named:
        return False  # the title is about another product
    if product in post.get("listed_on", []):
        return True
    slug = post["url"].rstrip("/").rsplit("/", 1)[-1].lower()
    haystack = f"{post['title'].lower()} {slug} {post.get('summary', '')}"
    return any(k.lower() in haystack for k in fcfg.get("keywords", []))


def display_date(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%b %-d")


def main() -> int:
    cfg = load_config()
    bcfg = cfg["blogs"]
    log(f"fetching blog feed: {bcfg['feed']}")
    posts = parse_feed(http_get(bcfg["feed"], retries=int(bcfg.get("max_retries", 4)),
                                backoff=float(bcfg.get("retry_backoff_seconds", 3))))
    log(f"  feed: {len(posts)} posts")
    by_url = {state.norm_url(p["url"]): p for p in posts}
    for product, listed in fetch_listings(bcfg.get("product_pages") or {}).items():
        for post in listed:
            known = by_url.get(state.norm_url(post["url"]))
            if known:
                # The listing shows the publish date; the feed only has a last-updated timestamp.
                known.update(date=post["date"], source="product-page")
                known["listed_on"].append(product)
            else:
                by_url[state.norm_url(post["url"])] = post

    recent_count = int(bcfg.get("recent_count", 8))
    history = state.load("blogs.json")
    products: dict[str, dict] = {}
    for key, fcfg in bcfg["filters"].items():
        fresh = [p for p in by_url.values() if belongs(p, key, fcfg)]
        merged = state.merge_blog_posts(history.get(key, []), fresh)
        history[key] = merged
        live = {state.norm_url(p["url"]): p["source"] for p in fresh}
        products[key] = {
            "display": fcfg.get("display", key.title()),
            "recent": [{"date": p["date"], "display_date": display_date(p["date"]), "title": p["title"],
                        "url": p["url"], "source": live.get(state.norm_url(p["url"]), "history")}
                       for p in merged[:recent_count]],
        }
        log(f"  {key}: {len(fresh)} live + history -> {len(products[key]['recent'])} "
            f"(newest {merged[0]['date'] if merged else '-'})")
    if state.save("blogs.json", history):
        log("  updated state/blogs.json")

    write_output("blogs.json", {"products": products, "feed": bcfg["feed"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
