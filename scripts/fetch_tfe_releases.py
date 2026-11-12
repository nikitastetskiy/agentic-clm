#!/usr/bin/env python3
"""Fetch the Terraform Enterprise releases of the deck's history window from the public docs.

Terraform Enterprise is not on the releases API. Its releases page links one page per ``N.M`` series
(``…/releases/2.0.x``), and a series page lists every version as a heading (``id="2-0-3"``) with its
date beside it and its notes below. This reads the newest series pages and writes, for every release
in the window: version, date, link, and the notes grouped by section (what the deck's wording is
derived from).

Augments data/releases.json -> ``terraform_enterprise`` in place. Run AFTER fetch_releases.py. A page
that cannot be fetched leaves ``releases`` empty and ``status: "unavailable"``: the planner then says
that the slide has no data.
"""
from __future__ import annotations

import html as html_lib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import htmldoc  # noqa: E402
from lib.common import DATA_DIR, http_get, load_config, log, month_label, window_start  # noqa: E402

RELEASES_JSON = DATA_DIR / "releases.json"
RELEASES_BASE = "https://developer.hashicorp.com/terraform/enterprise/releases"
MAX_SERIES = 4           # series pages read per run
SERIES_LINK_RE = re.compile(r"/terraform/enterprise/releases/(\d+)\.(\d+)\.x")  # 2.0.x, 1.2.x …
VERSION_HEADING_RE = re.compile(r'id="(\d+-\d+-\d+)"')         # <h2 id="2-0-3">
VER_DATE_RE = re.compile(r"(\d+\.\d+\.\d+)\s+(\d{4}-\d{2}-\d{2})")   # rendered "2.0.3 2026-06-03"
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"(?is)<(script|style)\b.*?</\1>")


def _strip_tags(html_text: str) -> str:
    s = _SCRIPT_RE.sub(" ", html_text)  # drop __NEXT_DATA__/style so only rendered prose remains
    return html_lib.unescape(_TAG_RE.sub(" ", s))


def semver_series(main_html: str) -> list[tuple[int, int]]:
    """All ``N.M`` semver series linked on the page, newest first (e.g. [(2,0),(1,2),(1,1),(1,0)])."""
    pairs = {(int(a), int(b)) for a, b in SERIES_LINK_RE.findall(main_html)}
    return sorted(pairs, reverse=True)


def parse_series_page(html_text: str, series: str) -> list[dict]:
    """A series page (…/releases/<series>.x) -> its releases, newest first, with their real dates.

    Only versions that appear BOTH as a release heading (``id="2-0-3"``) AND immediately ahead of an
    ISO date in the rendered text are kept, so the component-versions table (versions with no adjacent
    date) and stray numbers can't be mistaken for releases."""
    headings = {h.replace("-", ".") for h in VERSION_HEADING_RE.findall(html_text)}
    rows: dict[str, dict] = {}
    for ver, date in VER_DATE_RE.findall(_strip_tags(html_text)):
        if ver in headings and ver not in rows:
            rows[ver] = {"version": ver, "date": date, "month": date[:7], "month_label": month_label(date),
                         "url": f"{RELEASES_BASE}/{series}.x#{ver.replace('.', '-')}"}
    return sorted(rows.values(), key=lambda r: r["date"], reverse=True)


SECTION_ITEMS = 8        # notes kept per section in ``sections``
SECTION_ITEM_CHARS = 420


def parse_series_sections(html_text: str) -> dict[str, dict[str, list[dict]]]:
    """version -> its release notes grouped by section ("Features", "Bug Fixes", "Security", …), read
    from the page structure: each release is a heading whose id is its version (``2-0-8``), followed
    by one sub-heading + list per section. Complements parse_series_page (versions + dates)."""
    doc = htmldoc.parse(html_text)
    version_id = re.compile(r"\d+-\d+-\d+")
    out: dict[str, dict[str, list[dict]]] = {}
    for head in htmldoc.headings_with_id(doc, version_id.pattern):
        body = htmldoc.entry_body(head, lambda h: bool(version_id.fullmatch(h.get("id") or "")))
        groups = htmldoc.sections(body)
        groups.pop("", None)  # preamble: "Last required release", container digests
        out[head.get("id").replace("-", ".")] = {
            name: [{**item, "text": item["text"][:SECTION_ITEM_CHARS]} for item in items[:SECTION_ITEMS]]
            for name, items in groups.items()}
    return out


def fetch_releases(page: str, since: str) -> list[dict]:
    """Every release on the newest series pages, newest first, each with its notes by section."""
    series = semver_series(http_get(page).decode("utf-8", "replace"))
    log(f"  series found: {['%d.%d.x' % s for s in series]}")
    rows: dict[str, dict] = {}
    for major, minor in series[:MAX_SERIES]:
        name = f"{major}.{minor}"
        try:
            html = http_get(f"{RELEASES_BASE}/{name}.x").decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 — one unreadable series page does not lose the others
            log(f"    series {name}.x fetch failed: {exc}")
            continue
        notes = parse_series_sections(html)
        new = [r for r in parse_series_page(html, name) if r["version"] not in rows]
        rows.update((r["version"], {**r, "sections": notes.get(r["version"], {})}) for r in new)
        log(f"    {name}.x: {[r['version'] for r in new]}")
        # An older series is still maintained alongside the newest one (1.2.x patches ship next to
        # 2.0.x), so keep walking until a series has nothing inside the history window.
        if rows and not any(r["date"] >= since for r in new):
            break
    return sorted(rows.values(), key=lambda r: r["date"], reverse=True)


def main() -> int:
    cfg = load_config()
    tfe_cfg = cfg.get("terraform_enterprise", {})
    page = tfe_cfg.get("releases_page")
    if not page:
        log("no terraform_enterprise.releases_page configured")
        return 1
    if not RELEASES_JSON.exists():
        log("releases.json not found — run fetch_releases.py first")
        return 1
    since = window_start(int(cfg.get("releases", {}).get("history_months", 6)))

    log(f"fetching TFE releases page: {page}")
    try:
        rows = fetch_releases(page, since)
    except Exception as exc:  # noqa: BLE001 — the deck is then built without this slide, and says so
        log(f"  TFE page fetch failed: {exc}")
        rows = []
    releases = [r for r in rows if r["date"] >= since]
    log(f"  {len(releases)} TFE release(s) in the history window (since {since})"
        + (f"; newest v{rows[0]['version']} ({rows[0]['date']})" if rows else ""))

    data = json.loads(RELEASES_JSON.read_text(encoding="utf-8"))
    data["terraform_enterprise"] = {"display": tfe_cfg.get("display", "Terraform Enterprise"),
                                    "releases_page": page, "status": "ok" if rows else "unavailable",
                                    "releases": releases}
    RELEASES_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"updated {RELEASES_JSON.name} -> terraform_enterprise")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
