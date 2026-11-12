#!/usr/bin/env python3
"""Attach each release's public changelog section to its entry in data/releases.json.

For every release in a product's ``releases`` window that names a ``changelog_url`` this records:
  * ``sections`` — the bullets of its ``## <version>`` section grouped by category (what the deck's
    fixed labels and the enrichment step are derived from), and
  * the release DATE printed in the changelog heading (``## 1.16.5 (September 30, 2026)``), which is
    the date the deck shows. The releases API reports when the binaries were published, which can be
    days later; a release must not move to another month column because of that.

Each changelog file is fetched once. Nothing is invented: a version whose section cannot be found
keeps what the releases API said about it. Run AFTER fetch_releases.py.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.common import DATA_DIR, MONTH_NAMES, http_get, log, month_label  # noqa: E402

RELEASES_JSON = DATA_DIR / "releases.json"

CATEGORY_RE = re.compile(r"^(?P<name>[A-Z][A-Z /]+[A-Z]):\s*$")
BULLET_RE = re.compile(r"^[*-]\s+(?P<text>.*)$")
PR_REF_RE = re.compile(r"\s*\(\[#\d+\]\([^)]*\)\)")          # ([#38665](https://…))
GH_REF_RE = re.compile(r"\s*\[GH-\d+\]")                       # [GH-1234]
BARE_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")            # [text](url) -> text


def raw_url(url: str) -> str:
    """github.com/<o>/<r>/blob/<ref>/<path>  ->  raw.githubusercontent.com/<o>/<r>/<ref>/<path>."""
    m = re.match(r"https?://github\.com/([^/]+)/([^/]+)/blob/(.+)$", url or "")
    if m:
        return f"https://raw.githubusercontent.com/{m.group(1)}/{m.group(2)}/{m.group(3)}"
    return url


def section_for(changelog: str, version: str) -> str | None:
    """Return the text between ``## <version>`` and the next ``## `` header (exclusive)."""
    lines = changelog.splitlines()
    start = None
    head = re.compile(r"^##\s+v?" + re.escape(version) + r"\b")
    for i, line in enumerate(lines):
        if head.match(line):
            start = i
            break
    if start is None:
        return None
    out = []
    for line in lines[start + 1:]:
        if re.match(r"^##\s+\S", line):
            break
        out.append(line)
    return "\n".join(out).strip("\n")


HEADING_DATE_RE = re.compile(r"\(\s*([A-Z][a-z]+)\s+(\d{1,2}),\s*(\d{4})\s*\)")
SECTION_BULLETS = 8     # bullets kept per category in ``sections``
SECTION_BULLET_CHARS = 320


def heading_date(changelog: str, version: str) -> str | None:
    """The release date printed in a version's heading, as ISO: '## 1.16.5 (September 30, 2026)' ->
    '2026-09-30'. None for an unreleased or undated heading."""
    head = re.compile(r"^##\s+v?" + re.escape(version) + r"\b(.*)$", re.M)
    m = head.search(changelog)
    d = HEADING_DATE_RE.search(m.group(1)) if m else None
    if not d or d.group(1) not in MONTH_NAMES:
        return None
    return f"{int(d.group(3)):04d}-{MONTH_NAMES.index(d.group(1)) + 1:02d}-{int(d.group(2)):02d}"


def clean_text(text: str) -> str:
    """Strip PR/issue refs and markdown noise from a bullet, keeping the whole sentence."""
    text = PR_REF_RE.sub("", text)
    text = GH_REF_RE.sub("", text)
    text = BARE_LINK_RE.sub(r"\1", text)
    return re.sub(r"\s+", " ", text.replace("**", "").replace("`", "")).strip()


def grouped_sections(section: str) -> dict[str, list[dict]]:
    """The section's bullets by category, cleaned and size-capped (for labels and enrichment). Items
    share the shape the docs-page fetchers emit: ``{"lead", "text", "enterprise"}``."""
    out: dict[str, list[dict]] = {}
    for name, bullets in parse_categories(section).items():
        kept = [clean_text(b)[:SECTION_BULLET_CHARS] for b in bullets[:SECTION_BULLETS]]
        out[name] = [{"lead": "", "text": b, "enterprise": False} for b in kept if b]
    return {k: v for k, v in out.items() if v}


def parse_categories(section: str) -> dict[str, list[str]]:
    """Group the section's bullets by their CATEGORY: heading. Joins wrapped bullet lines."""
    cats: dict[str, list[str]] = {}
    current: str | None = None
    buf: list[str] = []

    def flush():
        if current and buf:
            cats.setdefault(current, []).append(" ".join(b.strip() for b in buf).strip())

    for line in section.splitlines():
        cm = CATEGORY_RE.match(line.strip())
        if cm:
            flush()
            buf = []
            current = cm.group("name").strip()
            continue
        bm = BULLET_RE.match(line.strip())
        if bm:
            flush()
            buf = [bm.group("text")]
        elif buf and line.strip():
            buf.append(line)  # continuation of the current bullet
        elif not line.strip():
            flush()
            buf = []
    flush()
    return cats


def main() -> int:
    if not RELEASES_JSON.exists():
        log("releases.json not found — run fetch_releases.py first")
        return 1
    data = json.loads(RELEASES_JSON.read_text(encoding="utf-8"))

    cache: dict[str, str | None] = {}

    def changelog_text(url: str) -> str | None:
        key = raw_url(url)
        if key not in cache:
            try:
                cache[key] = http_get(key).decode("utf-8", "replace")
                log(f"  fetched {key}")
            except Exception as exc:  # noqa: BLE001 — best-effort: the entry keeps its API data
                log(f"  changelog fetch failed: {key}: {exc}")
                cache[key] = None
        return cache[key]

    described = redated = 0
    for prod in data.get("products", {}).values():
        for entry in prod.get("releases", []):
            ver, url = entry["version"], entry.get("changelog_url")
            # A pre-release has no section of its own (the heading reads "## 1.17.0 (Unreleased)").
            text = changelog_text(url) if url and not entry.get("is_prerelease") else None
            if not text:
                continue
            section = section_for(text, ver)
            if not section:
                log(f"  {ver}: no '## {ver}' section in changelog")
                continue
            entry["sections"] = grouped_sections(section)
            described += 1
            released = heading_date(text, ver)
            if released and released != entry["date"]:
                entry.update(date=released, date_source="changelog", month=released[:7],
                             month_label=month_label(released))
                redated += 1
        prod.get("releases", []).sort(key=lambda e: (e["date"], e["version"]), reverse=True)

    RELEASES_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"changelog sections: {described} version(s); {redated} dated from the changelog heading")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
