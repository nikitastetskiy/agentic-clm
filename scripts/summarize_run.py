#!/usr/bin/env python3
"""Print a human-readable summary of what a pipeline run changed in the deck.

Reads the deterministic edit reports (release / blog / event), the latest verify-*.txt
gate and any review-*.md, then prints a compact table of actions plus an explicit
"please review before sharing" caution and the path to the finished deck.

Usage:
    python scripts/summarize_run.py [WORKING_COPY.pptx]

If WORKING_COPY is omitted the newest deck in output/ is used. The script never fails the
run: missing reports are reported as "not run" rather than raising.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
OUTPUT_DIR = REPO_ROOT / "output"
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _slide_groups():
    """Derive (release, blog, webinar, featured) slide groups from config/slide-map.yaml so the
    summary always matches the deck the editors actually targeted — slide positions/labels drift
    per deck (e.g. this deck added a 'Terraform Core' slide, shifting the rest). Falls back to the
    historic hard-coded sets if the map can't be read."""
    rel, blog, web, feat = [], [], [], []
    try:
        from lib.common import load_config
        slides = (load_config("slide-map.yaml") or {}).get("slides", []) or []
    except Exception:  # noqa: BLE001 — summary must never fail the run
        slides = []
    for s in slides:
        n, name = s.get("n"), s.get("name", "")
        typ, src, prod = s.get("type"), s.get("source"), s.get("product")
        if prod == "featured" or typ == "in_person_event":
            feat.extend(n if isinstance(n, list) else [n])
        elif isinstance(n, list):
            continue
        elif typ == "recent_blogs" or src == "blogs":
            blog.append((n, name))
        elif typ == "webinars":
            web.append((n, name))
        elif typ == "release_updates" or src in ("releases", "terraform_enterprise"):
            rel.append((n, name))
    return rel, blog, web, [n for n in feat if n is not None]


_REL, _BLOG, _WEB, _FEAT = _slide_groups()

# Slide groups, in the deck's reading order, mapped to a friendly label. Derived from slide-map.yaml
# (above); the literals here are only a fallback if the map is missing/unreadable.
RELEASE_SLIDES = _REL or [
    (7, "Terraform Core releases"),
    (8, "Terraform Enterprise releases"),
    (12, "Vault Enterprise releases"),
    (13, "HCP Vault releases"),
]
BLOG_SLIDES = _BLOG or [(10, "Terraform blogs"), (14, "Vault blogs")]
WEBINAR_SLIDES = _WEB or [(11, "Terraform webinars"), (15, "Vault webinars")]
FEATURED_SLIDES = _FEAT or [19, 20, 21, 22]


def _color(enabled: bool):
    if not enabled:
        return (lambda s: s,) * 4
    bold = lambda s: f"\033[1m{s}\033[0m"
    blue = lambda s: f"\033[1;34m{s}\033[0m"
    yellow = lambda s: f"\033[1;33m{s}\033[0m"
    dim = lambda s: f"\033[2m{s}\033[0m"
    return bold, blue, yellow, dim


def load_json(name: str) -> dict | None:
    path = DATA_DIR / name
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return None


def latest(pattern: str) -> Path | None:
    matches = [
        m for m in glob.glob(str(OUTPUT_DIR / pattern))
        if not os.path.basename(m).startswith("~$")  # skip PowerPoint lock files
    ]
    matches.sort(key=os.path.getmtime, reverse=True)
    return Path(matches[0]) if matches else None


def plural(n: int, one: str, many: str | None = None) -> str:
    return one if n == 1 else (many or one + "s")


def release_rows(report: dict | None) -> list[tuple[str, str, str]]:
    rows = []
    slides = (report or {}).get("slides", {})
    for n, label in RELEASE_SLIDES:
        s = slides.get(str(n))
        if report is None:
            action = "not run"
        elif s is None:
            action = "not run"
        elif s.get("fallback"):
            action = "handed to AI (ambiguous) — verify by hand"
        elif s.get("added"):
            action = f"refreshed → new v{s['added'].get('version', '?')} (latest column)"
        elif s.get("shifted"):
            action = f"columns shifted → v{s['shifted'].get('version', '?')} (latest)"
        elif s.get("reconciled"):
            cnt = len(s["reconciled"])
            action = f"date-corrected {cnt} {plural(cnt, 'entry', 'entries')}"
        elif s.get("refreshed"):
            action = "refreshed latest column"
        elif s.get("changed"):
            action = "updated"
        else:
            action = "unchanged (no fresh release)"
        rows.append((str(n), label, action))
    return rows


def blog_rows(report: dict | None) -> list[tuple[str, str, str]]:
    rows = []
    slides = (report or {}).get("slides", {})
    for n, label in BLOG_SLIDES:
        s = slides.get(str(n))
        if s is None:
            action = "not run"
        else:
            inserted = s.get("inserted") or []
            if s.get("changed") and inserted:
                action = f"{len(inserted)} new {plural(len(inserted), 'post')} added at top"
            else:
                action = "unchanged (no new posts)"
        rows.append((str(n), label, action))
    return rows


def webinar_rows(report: dict | None) -> list[tuple[str, str, str]]:
    rows = []
    slides = (report or {}).get("slides", {})
    for n, label in WEBINAR_SLIDES:
        s = slides.get(str(n))
        if s is None:
            action = "not run"
        elif s.get("hidden"):
            action = "hidden (no upcoming webinars)"
        else:
            filled = s.get("filled") or []
            backfilled = len(s.get("backfilled") or [])
            action = f"{len(filled)} {plural(len(filled), 'slot')} filled"
            if backfilled:
                action += f" ({backfilled} backfilled)"
            flags = s.get("flags") or []
            if flags:
                action += f", {len(flags)} flag{'s' if len(flags) != 1 else ''} relabeled"
        rows.append((str(n), label, action))
    return rows


def featured_rows(report: dict | None) -> tuple[list[tuple[str, str, str]], int, int]:
    rows = []
    shown = hidden = 0
    slides = (report or {}).get("slides", {})
    for n in FEATURED_SLIDES:
        s = slides.get(str(n))
        if s is None:
            action = "not run"
        elif s.get("hidden"):
            action = "hidden (no event)"
            hidden += 1
        else:
            title = (s.get("title") or "").strip()
            date = (s.get("date") or "").strip()
            action = title + (f" — {date}" if date else "") if title else "filled"
            if s.get("image_swapped"):
                action += "  [photo updated]"
            shown += 1
        rows.append((str(n), "Featured event", action))
    return rows, shown, hidden


def render_table(rows: list[tuple[str, str, str]], dim) -> tuple[list[str], int]:
    headers = ("Slide", "Area", "Action")
    cols = list(zip(headers, *rows)) if rows else [(h,) for h in headers]
    widths = [max(len(str(c)) for c in col) for col in cols]
    widths = [min(w, cap) for w, cap in zip(widths, (5, 40, 52))]
    total = 2 + sum(widths) + 2 * (len(widths) - 1)

    def fmt(cells):
        return "  " + "  ".join(str(c).ljust(w)[:w] for c, w in zip(cells, widths))

    lines = [dim(fmt(headers)), dim(fmt(["-" * w for w in widths]))]
    for r in rows:
        lines.append(fmt(r))
    return lines, total


def verify_status() -> tuple[str, Path | None]:
    path = latest("verify-*.txt")
    if not path:
        return ("not run", None)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ("unreadable", path)
    m = re.search(r"OVERALL:\s*(PASS|FAIL)", text)
    return (m.group(1) if m else "unknown", path)


def main(argv: list[str]) -> int:
    deck = Path(argv[1]).resolve() if len(argv) > 1 and argv[1] else None
    if deck is None:
        deck = latest("*.pptx")
        deck = deck.resolve() if deck else None

    use_color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    bold, blue, yellow, dim = _color(use_color)

    rel = load_json("release-edit-report.json")
    blog = load_json("blog-edit-report.json")
    evt = load_json("event-edit-report.json")

    rows: list[tuple[str, str, str]] = []
    rows += release_rows(rel)
    rows += blog_rows(blog)
    rows += webinar_rows(evt)
    feat_rows, shown, hidden = featured_rows(evt)
    rows += feat_rows

    width = 91
    bar = "─" * width
    out = []
    out.append(blue(bar))
    out.append(blue("  RUN SUMMARY — what was changed in this deck"))
    out.append(blue(bar))
    table, table_w = render_table(rows, dim)
    width = max(width, table_w)
    bar = "─" * width
    out[0] = blue(bar)
    out[2] = blue(bar)
    out += table
    if FEATURED_SLIDES and evt is not None:
        _frange = (f"{min(FEATURED_SLIDES)}–{max(FEATURED_SLIDES)}"
                   if len(FEATURED_SLIDES) > 1 else str(FEATURED_SLIDES[0]))
        out.append(dim(f"  Featured events ({_frange}): {shown} shown, {hidden} hidden"))
        photos = sum(1 for n in FEATURED_SLIDES
                     if (evt.get("slides", {}).get(str(n)) or {}).get("image_swapped"))
        if photos:
            city = sum(1 for n in FEATURED_SLIDES
                       if ((evt.get("slides", {}).get(str(n)) or {}).get("image_source") or {}).get("kind") == "city")
            suffix = f"; {city} city photo{'s' if city != 1 else ''}" if city else ""
            out.append(dim(f"  Event photos refreshed:  {photos}{suffix}"))
    out.append("")

    status, vpath = verify_status()
    badge = {"PASS": "✅ PASS", "FAIL": "⚠️  FAIL", "not run": "— not run"}.get(status, f"? {status}")
    vp = f"  ({vpath.relative_to(REPO_ROOT)})" if vpath else ""
    out.append(f"  Verification gate:  {badge}{dim(vp)}")
    review = latest("review-*.md")
    if review:
        out.append(dim(f"  Reviewer notes:     {review.relative_to(REPO_ROOT)}"))
    elapsed = os.environ.get("RUN_ELAPSED_SEC", "")
    if elapsed.isdigit():
        secs = int(elapsed)
        out.append(f"  Time taken:         {secs // 60}m {secs % 60}s")
    out.append("")

    out.append(yellow("  ⚠️  PLEASE REVIEW BEFORE SHARING"))
    out.append("  This deck was updated automatically. Open it and double-check the")
    out.append("  refreshed slides — release versions, dates, blog/event titles and links.")
    out.append("  Your original file was NOT modified; this is a fresh, timestamped copy.")
    out.append("")
    out.append(bold("  📄  Your updated deck:"))
    if deck and deck.exists():
        out.append(bold(f"      {deck}"))
    elif deck:
        out.append(yellow(f"      {deck}  (expected file not found!)"))
    else:
        out.append(yellow("      (no deck found in output/)"))
    out.append(blue(bar))

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
