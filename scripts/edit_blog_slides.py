#!/usr/bin/env python3
"""Deterministic editor for the recent-blogs slides (9 = Terraform, 13 = Vault).

Replaces the AI build agent's blog work. Each blog slide has 8 "blog entry" text boxes laid out
newest-first. The rolling standard is **insert-and-shift**: genuinely-new posts from
`data/blogs.json` (those not already on the slide) are inserted at the TOP, every existing entry
shifts DOWN one position, and the oldest entries fall off the bottom — so all 8 boxes always stay
filled and no curated post is overwritten or duplicated. When there are no new posts the slide is
left byte-stable.

A blog-entry shape's runs are: a DATE run (e.g. "      June 2 – "), a "See here" run carrying the
post hyperlink, then one or more TITLE runs. Writing an entry:
  * rewrites the date run as `<leading-ws>` + `<Month D>` + `<separator>` (the TARGET box's own
    whitespace/separator are preserved — only the date text changes),
  * repoints the "See here" hyperlink to the post URL via a FRESH relationship (shared-rId safe),
  * writes the full post title into the first title run and removes the surplus title runs.

Everything else (formatting, the static topic chips) is left byte-stable. Writes
`data/blog-edit-report.json`. Operates in place on a working copy; refuses to touch the original.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path
from xml.dom import minidom

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.common import DATA_DIR, load_config, log  # noqa: E402
from lib import pptx_edit as px  # noqa: E402

DATE_SPLIT_RE = re.compile(r"^(\s*).*?(\s*[\u2013\u2014-]\s*)$")  # leading ws ... " – " separator
SEE_TOKENS = {"see", "here", "see here", "see here "}


def fmt_date(iso: str) -> str:
    """'2026-06-16' -> 'June 16' (full month, no leading zero) to match the deck's style."""
    d = date.fromisoformat(iso)
    return f"{d.strftime('%B')} {d.day}"


_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}


def _display_to_iso(disp: str, year: int) -> str | None:
    """'June 9' (+ a reference year) -> '2026-06-09' for date-sorting existing entries."""
    m = re.match(r"\s*([A-Za-z]+)\.?\s+(\d{1,2})", disp or "")
    if not m:
        return None
    mon = _MONTHS.get(m.group(1).lower())
    return f"{year:04d}-{mon:02d}-{int(m.group(2)):02d}" if mon else None


def _entry_date_key(item: dict, year: int) -> str:
    """Sortable date for a blog entry: its ISO `date` (fresh posts) or parsed `date_display`."""
    return item.get("date") or _display_to_iso(item.get("date_display", ""), year) or "0000-00-00"


def is_blog_entry(sp) -> bool:
    _, y = px.shape_xy(sp)
    if y is None or y < 1.3:
        return False
    for run in sp.getElementsByTagName("a:r"):
        if px.run_hyperlink(run) is not None and px.run_text(run).strip().lower() in SEE_TOKENS:
            return True
    return False


def _see_indices(runs) -> list[int]:
    return [i for i, r in enumerate(runs)
            if px.run_hyperlink(r) is not None and px.run_text(r).strip().lower() in SEE_TOKENS]


def norm_url(u: str) -> str:
    """Normalize a blog URL for duplicate detection (locale + scheme + trailing-slash insensitive)."""
    u = (u or "").lower().split("://", 1)[-1]
    if u.startswith("www."):
        u = u[4:]
    return u.replace("/en/", "/").rstrip("/")


def entry_url(sp, rid_target: dict) -> str | None:
    for run in sp.getElementsByTagName("a:r"):
        hl = px.run_hyperlink(run)
        if hl is not None and px.run_text(run).strip().lower() in SEE_TOKENS:
            return rid_target.get(hl.getAttribute("r:id"))
    return None


def read_entry(sp, rid_target: dict) -> dict:
    """Extract an existing blog entry's displayed content so it can be SHIFTED to another box intact."""
    runs = sp.getElementsByTagName("a:r")
    date_disp = ""
    if runs:
        old = px.run_text(runs[0])
        m = DATE_SPLIT_RE.match(old)
        date_disp = (old[len(m.group(1)):len(old) - len(m.group(2))] if m else old).strip()
    see = _see_indices(runs)
    title = "".join(px.run_text(r) for r in runs[max(see) + 1:]).strip() if see else ""
    return {"date_display": date_disp, "title": title, "url": entry_url(sp, rid_target)}


def write_entry(sp, rels_dom, item: dict) -> dict:
    """Write a uniform entry dict (date_display / title / url) into a blog-entry box, preserving the
    box's own date whitespace+separator. Repoints the hyperlink only when the item carries a url."""
    runs = sp.getElementsByTagName("a:r")
    if not runs:
        return {"ok": False, "why": "no runs"}
    old = px.run_text(runs[0])
    m = DATE_SPLIT_RE.match(old)
    prefix, sep = (m.group(1), m.group(2)) if m else ("      ", " \u2013 ")
    px.set_run_text(runs[0], f"{prefix}{item['date_display']}{sep}")
    see = _see_indices(runs)
    if not see:
        return {"ok": False, "why": "no See-here link run"}
    if item.get("url"):
        new_rid = px.add_rel(rels_dom, item["url"])
        for i in see:
            px.run_hyperlink(runs[i]).setAttribute("r:id", new_rid)
    title_runs = runs[max(see) + 1:]
    if title_runs:
        px.set_run_text(title_runs[0], item["title"])
        for r in title_runs[1:]:
            px.remove_node(r)
    return {"ok": True, "date": item["date_display"], "title": item["title"]}


def edit_slide(slide_file: Path, rels_file: Path, posts: list[dict]) -> dict:
    """Insert-and-shift: new posts -> top, existing entries shift down, oldest fall off, all 8 boxes
    stay filled. No-op (byte-stable) when there are no genuinely-new posts."""
    dom = minidom.parse(str(slide_file))
    rels_dom, rid_target = px.load_rels(rels_file)
    entries = [sp for sp in dom.getElementsByTagName("p:sp") if is_blog_entry(sp)]
    existing = [read_entry(sp, rid_target) for sp in entries]
    existing_urls = {norm_url(e["url"]) for e in existing if e["url"]}
    existing_titles = {e["title"].strip().lower() for e in existing if e["title"]}

    fresh = []
    for p in posts:  # blogs.json is newest-first
        if norm_url(p["url"]) in existing_urls or p["title"].strip().lower() in existing_titles:
            continue
        fresh.append({"date": p["date"], "date_display": fmt_date(p["date"]),
                      "title": p["title"], "url": p["url"]})

    changed = bool(fresh)
    results: list[dict] = []
    pruned: list[str] = []
    if changed:
        # Order by DATE (newest-first), not the source slide's position: the deck's blog boxes are not
        # always date-sorted, so a plain position shift would push a RECENT post off the bottom while
        # keeping an older one (e.g. the June 9 "zero trust" post sat at slot 8 below May posts). Sort
        # so the 8 boxes always hold the 8 most-recent posts.
        ref_year = int(posts[0]["date"][:4]) if posts else date.today().year
        merged = sorted(fresh + existing, key=lambda e: _entry_date_key(e, ref_year),
                        reverse=True)[:len(entries)]
        for sp, item in zip(entries, merged):
            results.append(write_entry(sp, rels_dom, item))
        pruned = px.prune_orphan_hyperlinks(dom, rels_dom)
        slide_file.write_text(dom.toxml(), encoding="utf-8")
        rels_file.write_text(rels_dom.toxml(), encoding="utf-8")
    return {"entries_found": len(entries), "posts_available": len(posts),
            "inserted": [f["title"] for f in fresh], "results": results,
            "pruned_rels": pruned, "changed": changed}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("deck", help="working-copy .pptx (edited in place)")
    ap.add_argument("--report", default=str(DATA_DIR / "blog-edit-report.json"))
    ap.add_argument("--original", default=None, help="guard: refuse if deck == original")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    deck = Path(args.deck).resolve()
    if not deck.exists():
        raise SystemExit(f"deck not found: {deck}")
    if args.original and Path(args.original).resolve() == deck:
        raise SystemExit("refusing to edit the ORIGINAL deck in place")

    slide_map = load_config("slide-map.yaml")
    blogs = json.loads((DATA_DIR / "blogs.json").read_text(encoding="utf-8"))

    tmp = Path(tempfile.mkdtemp(prefix="blogedit-"))
    report = {"slides": {}, "any_change": False}
    try:
        px.unpack(deck, tmp)
        for entry in slide_map.get("slides", []):
            if entry.get("type") != "recent_blogs":
                continue
            product = entry.get("product")
            posts = (blogs.get("products", {}).get(product, {}) or {}).get("recent", [])
            ns = entry["n"]
            for n in (ns if isinstance(ns, list) else [ns]):
                sf = tmp / "ppt" / "slides" / f"slide{n}.xml"
                rf = tmp / "ppt" / "slides" / "_rels" / f"slide{n}.xml.rels"
                if not sf.exists():
                    continue
                srep = edit_slide(sf, rf, posts)
                srep["product"] = product
                report["slides"][str(n)] = srep
                report["any_change"] = report["any_change"] or srep["changed"]
        if report["any_change"] and not args.dry_run:
            out = px.pack(tmp, deck)
            if out.returncode != 0:
                log(f"pack failed: {out.stderr[-500:]}")
                return 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    for n, s in report["slides"].items():
        ins = s.get("inserted", [])
        log(f"slide {n} ({s['product']}): inserted {len(ins)} new post(s) at top, shifted "
            f"{s['entries_found']} entries (posts available: {s['posts_available']})"
            + ("" if s["changed"] else "; no new posts -> unchanged"))
        for t in ins:
            log(f"    + {t[:60]}")
    log(f"report -> {args.report}" + ("  [DRY-RUN, deck not written]" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
