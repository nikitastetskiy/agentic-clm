#!/usr/bin/env python3
"""Prepare the events data step.

The HashiCorp events page is behind Vercel bot protection, so it cannot be scraped with a
plain HTTP client. This script attempts a best-effort fetch; if blocked (the normal case), it
writes data/events.request.json describing exactly what the deck-events agent must gather via
its web_fetch tool and the JSON shape it should write to data/events.json.

This keeps the agent step deterministic and reviewable.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.common import DATA_DIR, http_get, load_config, log, write_output  # noqa: E402


def looks_blocked(html: bytes) -> bool:
    head = html[:4000].lower()
    return b"vercel security checkpoint" in head or b"just a moment" in head or len(html) < 1500


def main() -> int:
    cfg = load_config()
    ecfg = cfg["events"]
    pages = ecfg["pages"]
    today = date.today().isoformat()

    blocked = True
    try:
        html = http_get(pages["all_events"], retries=2, backoff=2)
        blocked = looks_blocked(html)
        log(f"events page fetched ({len(html)} bytes); blocked={blocked}")
    except Exception as exc:  # noqa: BLE001
        log(f"events page fetch failed ({exc}); will defer to agent")

    request = {
        "today": today,
        "status": "needs_agent_fetch" if blocked else "fetched_html_available",
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
                {"date": "YYYY-MM-DD", "display_date": "Month D", "title": "...", "location": "City", "url": "...", "description": "1-2 sentences", "language": "en"}
            ],
        },
    }

    write_output("events.request.json", request)
    # Ensure events.json exists (empty) so downstream steps never crash on a missing file.
    events_json = DATA_DIR / "events.json"
    if not events_json.exists():
        write_output("events.json", {"status": "pending_agent_fetch", "webinars": {"terraform": [], "vault": []}, "featured": []})
    log("events step prepared; agent will populate data/events.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
