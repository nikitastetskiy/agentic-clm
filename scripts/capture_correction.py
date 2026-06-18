#!/usr/bin/env python3
"""Capture a correction/learning so future runs honor it.

Appends a structured record to corrections/corrections.jsonl AND a human-readable bullet to
corrections/corrections.md under the right category heading. This is the mechanism by which human
rectifications become durable rules the pptx-orchestrator applies on the next run.

Usage:
  capture_correction.py "Vault webinar descriptions: max 1 line" \
      --category events --slide 14 --source human
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CORR_DIR = REPO_ROOT / "corrections"
MD = CORR_DIR / "corrections.md"
JSONL = CORR_DIR / "corrections.jsonl"

HEADINGS = {
    "global": "## Global",
    "releases": "## Releases (slides 7, 8, 11, 12)",
    "blogs": "## Blogs (slides 9, 13)",
    "events": "## Events / Webinars (slides 10, 14, 18\u201322)",
    "style": "## Style",
}


def append_md(rule: str, category: str, slide: int | None) -> None:
    heading = HEADINGS.get(category, HEADINGS["global"])
    text = MD.read_text(encoding="utf-8")
    suffix = f"  _(slide {slide})_" if slide else ""
    bullet = f"- {rule}{suffix}\n"
    idx = text.find(heading)
    if idx == -1:
        # Heading not found: append a new section.
        MD.write_text(text.rstrip() + f"\n\n{heading}\n{bullet}", encoding="utf-8")
        return
    # Insert the bullet at the end of that section (before the next "## " or EOF).
    start = idx + len(heading)
    nxt = text.find("\n## ", start)
    insert_at = len(text) if nxt == -1 else nxt
    new_text = text[:insert_at].rstrip("\n") + "\n" + bullet + text[insert_at:]
    MD.write_text(new_text, encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("rule")
    ap.add_argument("--category", default="global", choices=list(HEADINGS))
    ap.add_argument("--slide", type=int, default=None)
    ap.add_argument("--source", default="human", help="human | reviewer | <name>")
    args = ap.parse_args()

    CORR_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "rule": args.rule.strip(),
        "category": args.category,
        "slide": args.slide,
        "source": args.source,
    }
    with open(JSONL, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    append_md(args.rule.strip(), args.category, args.slide)
    print(f"recorded [{args.category}] {args.rule}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
