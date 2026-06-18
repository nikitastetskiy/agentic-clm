#!/usr/bin/env python3
"""Fetch recent HashiCorp blog posts and bucket them by product (Terraform / Vault).

Deterministic source for slides 9 (Terraform blogs) and 13 (Vault blogs).
The global Atom feed has no per-product tags, so posts are matched by keyword against
title + URL slug. Writes data/blogs.json.
"""
from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.common import http_get, load_config, log, write_output  # noqa: E402

ATOM = "{http://www.w3.org/2005/Atom}"


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
            dt = None
        entries.append(
            {
                "title": title,
                "url": url,
                "iso_date": dt.date().isoformat() if dt else None,
                "display_date": dt.strftime("%b %-d") if dt else None,
                "_dt": dt,
                "_slug": url.rsplit("/", 1)[-1].lower(),
                "_summary": re.sub(r"<[^>]+>", " ", summary).lower(),
            }
        )
    entries.sort(key=lambda x: x["_dt"] or datetime.min.replace(tzinfo=None), reverse=True)
    return entries


def matches(entry: dict, keywords: list[str]) -> bool:
    haystack = f"{entry['title'].lower()} {entry['_slug']} {entry['_summary']}"
    return any(k.lower() in haystack for k in keywords)


def main() -> int:
    cfg = load_config()
    bcfg = cfg["blogs"]
    log(f"fetching blog feed: {bcfg['feed']}")
    xml_bytes = http_get(
        bcfg["feed"],
        retries=int(bcfg.get("max_retries", 4)),
        backoff=float(bcfg.get("retry_backoff_seconds", 3)),
    )
    entries = parse_feed(xml_bytes)
    log(f"  parsed {len(entries)} entries")

    recent_count = int(bcfg.get("recent_count", 3))
    products: dict[str, dict] = {}
    for key, fcfg in bcfg["filters"].items():
        matched = [e for e in entries if matches(e, fcfg["keywords"])][:recent_count]
        products[key] = {
            "display": fcfg.get("display", key.title()),
            "recent": [
                {"date": e["iso_date"], "display_date": e["display_date"], "title": e["title"], "url": e["url"]}
                for e in matched
            ],
        }
        log(f"  {key}: {[e['title'][:48] for e in matched]}")

    write_output("blogs.json", {"products": products, "feed": bcfg["feed"]})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
