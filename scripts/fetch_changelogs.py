#!/usr/bin/env python3
"""Deterministically pre-fetch release changelogs and extract a one-line highlight per version.

This moves the *slow* part of the deck-data enrichment phase — web_fetching large CHANGELOG.md
files (with truncation/retry pain) and hunting for each version's section — out of the AI agent and
into fast, reproducible Python. The agent is then left with only a light wording polish over a
locally-cached excerpt (no web access), which is what cut deck-data from ~8 min toward ~2-3 min.

For every entry already in data/releases.json (each product's ``latest`` + ``recent``) this:
  1. resolves the entry's ``changelog_url`` to its raw.githubusercontent.com form,
  2. fetches each distinct changelog ONCE,
  3. slices the ``## <version>`` section,
  4. picks ONE notable bullet (FEATURES > ENHANCEMENTS > SECURITY > CHANGES > BUG FIXES) and cleans
     it into a concise factual sentence -> ``highlight`` (only if not already set),
  5. stores the cleaned section text on the product's ``latest`` as ``changelog_excerpt`` so the
     deck-data agent (or a human) can refine the wording WITHOUT re-fetching.

It also records, for every entry of the weekly deck's ``releases`` window:
  * ``sections`` — the section's bullets grouped by category (what the deck's fixed labels and the
    enrichment step are derived from), and
  * the release DATE printed in the changelog heading (``## 1.16.5 (September 30, 2026)``), which is
    the date the deck shows. The releases API reports when the binaries were published, which can be
    days later; a release must not move to another month column because of that.

``latest`` and ``recent[0]`` are kept consistent (the build renders ``latest`` into the newest
column). Never invents text: if a version's section can't be found, its highlight is left as-is and
``highlight_status`` is set to ``unavailable``.

Usage:  python scripts/fetch_changelogs.py            # augments data/releases.json in place
        python scripts/fetch_changelogs.py --force    # overwrite existing non-null highlights too
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.common import DATA_DIR, MONTH_NAMES, http_get, log, month_label  # noqa: E402

RELEASES_JSON = DATA_DIR / "releases.json"

# Category priority for picking the single most "showcase-worthy" change for a what's-new highlight.
# (New capabilities first; routine fixes last.) Matches the wording style the deck has used by hand.
CATEGORY_PRIORITY = [
    "BREAKING CHANGES",
    "NEW FEATURES",
    "FEATURES",
    "ENHANCEMENTS",
    "IMPROVEMENTS",
    "SECURITY",
    "CHANGES",
    "BUG FIXES",
    "DEPRECATIONS",
    "NOTES",
]
CATEGORY_RE = re.compile(r"^(?P<name>[A-Z][A-Z /]+[A-Z]):\s*$")
BULLET_RE = re.compile(r"^[*-]\s+(?P<text>.*)$")
PR_REF_RE = re.compile(r"\s*\(\[#\d+\]\([^)]*\)\)")          # ([#38665](https://…))
GH_REF_RE = re.compile(r"\s*\[GH-\d+\]")                       # [GH-1234]
BARE_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")            # [text](url) -> text
WORD_CAP = 26                                                   # hard cap so a highlight stays a one-liner


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


def clean_bullet(text: str) -> str:
    """Strip PR/issue refs and markdown noise; reduce to one concise factual sentence."""
    text = PR_REF_RE.sub("", text)
    text = GH_REF_RE.sub("", text)
    text = BARE_LINK_RE.sub(r"\1", text)          # [v0.23.1](url) -> v0.23.1
    text = text.replace("**", "").strip()
    text = re.sub(r"\s+", " ", text)
    # First sentence only (period followed by space/end). Keep abbreviations like e.g. intact-ish.
    m = re.search(r"^(.*?[.!?])(?:\s|$)", text)
    sentence = m.group(1) if m else text
    words = sentence.split()
    if len(words) > WORD_CAP:
        sentence = " ".join(words[:WORD_CAP]).rstrip(",;:") + "."
    return sentence.strip()


def pick_highlight(section: str) -> str | None:
    cats = parse_categories(section)
    for name in CATEGORY_PRIORITY:
        # tolerant match (e.g. "BUG FIXES" vs "BUGFIXES", "IMPROVEMENTS" present)
        for key in cats:
            if key.replace(" ", "") == name.replace(" ", "") and cats[key]:
                return clean_bullet(cats[key][0])
    # No recognised category headings — fall back to the first bullet anywhere.
    for line in section.splitlines():
        bm = BULLET_RE.match(line.strip())
        if bm:
            return clean_bullet(bm.group("text"))
    return None


def excerpt(section: str, limit: int = 1600) -> str:
    """A cleaned, size-capped copy of the section for the agent/human to refine wording from."""
    cleaned = PR_REF_RE.sub("", section)
    cleaned = GH_REF_RE.sub("", cleaned).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[:limit].rsplit("\n", 1)[0] + "\n…"
    return cleaned


def entries_by_version(prod: dict) -> dict[str, list[dict]]:
    """version -> every dict that describes it across the product's views (``latest``, ``recent``,
    ``month_window`` and the deck's ``releases`` window), so one changelog lookup updates them all."""
    out: dict[str, list[dict]] = {}
    views = [prod.get("latest")] + list(prod.get("recent", [])) + list(prod.get("month_window", []))
    for entry in views + list(prod.get("releases", [])):
        if entry and entry.get("version"):
            out.setdefault(entry["version"], []).append(entry)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="overwrite existing non-null highlights")
    args = ap.parse_args()

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
            except Exception as exc:  # noqa: BLE001 — best-effort; agent/human can fill gaps
                log(f"  changelog fetch failed: {key}: {exc}")
                cache[key] = None
        return cache[key]

    filled = redated = 0
    for prod in data.get("products", {}).values():
        latest_ver = (prod.get("latest") or {}).get("version")
        for ver, views in entries_by_version(prod).items():
            first = views[0]
            url = first.get("changelog_url")
            # A pre-release has no section of its own (the heading reads "## 1.17.0 (Unreleased)").
            if not url or first.get("is_prerelease"):
                continue
            text = changelog_text(url)
            if not text:
                for entry in views:
                    entry.setdefault("highlight_status", "unavailable")
                continue
            section = section_for(text, ver)
            if not section:
                log(f"  {ver}: no '## {ver}' section in changelog")
                for entry in views:
                    entry.setdefault("highlight_status", "unavailable")
                continue
            hl = pick_highlight(section)
            groups = grouped_sections(section)
            released = heading_date(text, ver)
            for entry in views:
                if hl and (args.force or not entry.get("highlight")):
                    entry["highlight"] = hl
                    entry.pop("highlight_status", None)
                entry["sections"] = groups
                if released and released != entry.get("date"):
                    entry.update(date=released, date_source="changelog", month=released[:7],
                                 month_label=month_label(released))
            if hl:
                filled += 1
            if released and first.get("date_source") == "changelog":
                redated += 1
            if ver == latest_ver and prod.get("latest"):
                prod["latest"]["changelog_excerpt"] = excerpt(section)
        prod.get("releases", []).sort(key=lambda e: (e.get("date", ""), e.get("version", "")),
                                      reverse=True)

    # Keep latest <-> recent[0] highlight consistent (the build renders latest into the new column).
    for prod in data.get("products", {}).values():
        latest = prod.get("latest")
        recent = prod.get("recent", [])
        if latest and recent and recent[0].get("date") == latest.get("date"):
            if latest.get("highlight"):
                recent[0]["highlight"] = latest["highlight"]

    RELEASES_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"changelog highlights: filled {filled} version(s); {redated} dated from the changelog heading")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
