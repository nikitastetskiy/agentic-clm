#!/usr/bin/env python3
"""Attach a product's public DOCS release notes to its entries in data/releases.json (Vault).

The Vault slide cites developer.hashicorp.com/vault/docs/updates/release-notes as its source and links
every version to its anchor on that page. This fetcher reads those pages and, for each release in the
deck's ``releases`` window:

  * sets ``url`` to the version's release-notes anchor (``…/release-notes#vault-2-1-2``). Older lines
    (the Enterprise LTS trains) live on versioned pages (``/docs/v1.21.x/…``) that carry notes for
    only some patches; a version without an anchor links to its line's page instead — never to an
    anchor that does not exist.
  * stores the notes grouped by section in ``sections`` (what the deck's fixed labels and the
    enrichment step are derived from), and
  * takes the ``Released:`` date printed on the page as the release date.

Run AFTER fetch_releases.py. Best-effort per page: a page that cannot be fetched leaves its entries
untouched (they keep the link from the releases API).
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import htmldoc  # noqa: E402
from lib.common import DATA_DIR, http_get_final, load_config, log, month_label  # noqa: E402

RELEASES_JSON = DATA_DIR / "releases.json"
RELEASED_RE = re.compile(r"Released:\s*(\d{4}-\d{2}-\d{2})")
SECTION_ITEMS = 8
SECTION_ITEM_CHARS = 420


def parse_notes_page(html_text: str, slug: str) -> dict[str, dict]:
    """version -> {"anchor", "released", "sections"} for every ``<slug>-X-Y-Z`` heading on the page."""
    doc = htmldoc.parse(html_text)
    version_id = re.compile(re.escape(slug) + r"-\d+-\d+-\d+")
    out: dict[str, dict] = {}
    for head in htmldoc.headings_with_id(doc, version_id.pattern):
        anchor = head.get("id")
        body = htmldoc.entry_body(head, lambda h: bool(version_id.fullmatch(h.get("id") or "")))
        released = None
        for node in body[:3]:
            m = RELEASED_RE.search(htmldoc.text_of(node))
            if m:
                released = m.group(1)
                break
        groups = htmldoc.sections(body)
        groups.pop("", None)  # the "Released:" line and any preamble
        out[anchor[len(slug) + 1:].replace("-", ".")] = {
            "anchor": anchor,
            "released": released,
            "sections": {name: [{**item, "text": item["text"][:SECTION_ITEM_CHARS]}
                                for item in items[:SECTION_ITEMS]]
                         for name, items in groups.items()},
        }
    return out


def main() -> int:
    cfg = load_config()
    if not RELEASES_JSON.exists():
        log("releases.json not found — run fetch_releases.py first")
        return 1
    data = json.loads(RELEASES_JSON.read_text(encoding="utf-8"))

    for key, pcfg in cfg["releases"]["products"].items():
        notes_cfg = pcfg.get("release_notes")
        prod = data.get("products", {}).get(key)
        if not notes_cfg or not prod:
            continue
        slug = pcfg["slug"]
        pages: dict[str, tuple[str, dict] | None] = {}

        def page(url: str) -> tuple[str, dict] | None:
            """(final url, parsed notes) for a release-notes page, fetched once; None if unavailable."""
            if url not in pages:
                try:
                    body, final = http_get_final(url, retries=2)
                    pages[url] = (final, parse_notes_page(body.decode("utf-8", "replace"), slug))
                    log(f"  {final}: notes for {sorted(pages[url][1], reverse=True)[:6]}")
                except Exception as exc:  # noqa: BLE001 — a missing page just means no anchors
                    log(f"  release notes unavailable: {url}: {exc}")
                    pages[url] = None
            return pages[url]

        log(f"fetching release notes: {slug}")
        current = page(notes_cfg["current"])
        linked = redated = 0
        for entry in prod.get("releases", []):
            ver = entry["version"]
            found = current if current and ver in current[1] else None
            if found is None and notes_cfg.get("versioned"):
                found = page(notes_cfg["versioned"].format(line=entry["line"]))
            if found is None:
                continue
            final_url, notes = found
            note = notes.get(ver)
            if note is None:
                entry["url"] = final_url          # the line's page; this patch has no notes of its own
                continue
            entry["url"] = f"{final_url}#{note['anchor']}"
            entry["sections"] = note["sections"]
            linked += 1
            if note["released"] and note["released"] != entry["date"]:
                entry.update(date=note["released"], date_source="release-notes",
                             month=note["released"][:7], month_label=month_label(note["released"]))
                redated += 1
        prod["releases"].sort(key=lambda e: (e["date"], e["version"]), reverse=True)
        log(f"  {slug}: {linked} version(s) linked to a release-notes anchor, {redated} re-dated")

    RELEASES_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
