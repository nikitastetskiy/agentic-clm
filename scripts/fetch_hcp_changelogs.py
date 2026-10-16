#!/usr/bin/env python3
"""Fetch the date-based product changelogs behind the HCP slides (HCP Terraform, HCP Vault Dedicated).

Each changelog is one docs page on which every entry is a heading whose ``id`` is its date
(``#2026-10-02``), followed by the changes shipped that day: a list of items (HCP Terraform) or
titled paragraphs (HCP Vault: ``Port 443 access`` + a sentence). Writes data/changelogs.json with, per
product, every entry inside the history window, newest first:

    {"date", "month_label", "url" (the entry's anchor), "items": [{"title", "text"}]}

Deterministic: one HTTP GET per page, no browser. A page that cannot be fetched or parsed is recorded
with ``status: "unavailable"`` and no entries, and the run fails so a silent gap never reaches a deck.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import htmldoc  # noqa: E402
from lib.common import http_get_final, load_config, log, month_label, window_start, write_output  # noqa: E402

DATE_ID = r"\d{4}-\d{2}-\d{2}"
ITEM_CHARS = 900


def parse_changelog(html_text: str, page_url: str, since: str) -> list[dict]:
    doc = htmldoc.parse(html_text)
    date_id = re.compile(DATE_ID)
    entries = []
    for head in htmldoc.headings_with_id(doc, DATE_ID):
        day = head.get("id")
        if day < since:
            continue
        groups = htmldoc.sections(
            htmldoc.entry_body(head, lambda h: bool(date_id.fullmatch(h.get("id") or ""))))
        items = [{"title": it["lead"], "text": it["text"][:ITEM_CHARS]} for it in groups.pop("", [])]
        for title, its in groups.items():
            text = " ".join(it["text"] for it in its)
            items.append({"title": title, "text": text[:ITEM_CHARS]})
        if items:
            entries.append({"date": day, "month": day[:7], "month_label": month_label(day),
                            "url": f"{page_url}#{day}", "items": items})
    return sorted(entries, key=lambda e: e["date"], reverse=True)


def main() -> int:
    cfg = load_config()
    since = window_start(int(cfg.get("releases", {}).get("history_months", 6)))
    products: dict[str, dict] = {}
    failed = []
    for key, ccfg in (cfg.get("changelogs") or {}).items():
        log(f"fetching changelog: {key} ({ccfg['url']})")
        entry = {"display": ccfg.get("display", key), "url": ccfg["url"], "status": "ok", "entries": []}
        try:
            body, final = http_get_final(ccfg["url"])
            entry["url"] = final
            entry["entries"] = parse_changelog(body.decode("utf-8", "replace"), final, since)
            if not entry["entries"]:
                raise RuntimeError("no dated entries found (page layout change?)")
            log(f"  {key}: {len(entry['entries'])} entries since {since}, newest {entry['entries'][0]['date']}")
        except Exception as exc:  # noqa: BLE001
            log(f"  {key}: FAILED — {exc}")
            entry["status"] = "unavailable"
            failed.append(key)
        products[key] = entry
    write_output("changelogs.json", {"window": {"since": since}, "products": products})
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
