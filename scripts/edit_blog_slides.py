#!/usr/bin/env python3
"""Deterministic editor for the recent-blogs slides (9 = Terraform, 13 = Vault).

Replaces the AI build agent's blog work. Each blog slide has several "blog entry" text boxes laid
out newest-first; the rolling standard (same as releases) is to **refresh the top N entries** with
the N newest posts from `data/blogs.json` and leave the older entries untouched as history.

A blog-entry shape's runs are: a DATE run (e.g. "      June 2 – "), a "See here" run carrying the
post hyperlink, then one or more TITLE runs. For each refreshed entry this editor:
  * rewrites the date run as `<leading-ws>` + `<Month D>` + `<separator>` (whitespace/sep preserved),
  * repoints the "See here" hyperlink to the post URL via a FRESH relationship (shared-rId safe),
  * writes the full post title into the first title run and removes the surplus title runs.

Everything else (formatting, the static topic chips, older entries) is left byte-stable. Writes
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


def is_blog_entry(sp) -> bool:
    _, y = px.shape_xy(sp)
    if y is None or y < 1.3:
        return False
    for run in sp.getElementsByTagName("a:r"):
        if px.run_hyperlink(run) is not None and px.run_text(run).strip().lower() in SEE_TOKENS:
            return True
    return False


def refresh_entry(sp, rels_dom, post: dict) -> dict:
    runs = sp.getElementsByTagName("a:r")
    if not runs:
        return {"ok": False, "why": "no runs"}

    # 1) date run (first run): preserve leading whitespace + separator, swap the date text.
    old = px.run_text(runs[0])
    m = DATE_SPLIT_RE.match(old)
    prefix, sep = (m.group(1), m.group(2)) if m else ("      ", " \u2013 ")
    px.set_run_text(runs[0], f"{prefix}{fmt_date(post['date'])}{sep}")

    # 2) the "See here" hyperlink run(s): repoint to the post URL via one fresh rel.
    see_idx = [i for i, r in enumerate(runs)
               if px.run_hyperlink(r) is not None and px.run_text(r).strip().lower() in SEE_TOKENS]
    if not see_idx:
        return {"ok": False, "why": "no See-here link run"}
    new_rid = px.add_rel(rels_dom, post["url"])
    for i in see_idx:
        px.run_hyperlink(runs[i]).setAttribute("r:id", new_rid)

    # 3) title runs (everything after the last See-here run): first carries the whole title; drop rest.
    title_runs = runs[max(see_idx) + 1:]
    if title_runs:
        px.set_run_text(title_runs[0], post["title"])
        for r in title_runs[1:]:
            px.remove_node(r)
    return {"ok": True, "date": fmt_date(post["date"]), "title": post["title"], "rid": new_rid}


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


def clear_entry(sp) -> None:
    """Blank a blog entry's visible text (used to drop a tail entry that duplicates a refreshed post)."""
    for at in sp.getElementsByTagName("a:t"):
        if at.firstChild:
            at.firstChild.data = ""


def edit_slide(slide_file: Path, rels_file: Path, posts: list[dict]) -> dict:
    dom = minidom.parse(str(slide_file))
    rels_dom, rid_target = px.load_rels(rels_file)
    entries = [sp for sp in dom.getElementsByTagName("p:sp") if is_blog_entry(sp)]
    n = min(len(posts), len(entries))
    refreshed = []
    refreshed_urls, refreshed_titles = set(), set()
    for i in range(n):
        res = refresh_entry(entries[i], rels_dom, posts[i])
        refreshed.append(res)
        if res.get("ok"):
            refreshed_urls.add(norm_url(posts[i]["url"]))
            refreshed_titles.add(posts[i]["title"].strip().lower())
    # De-dup: a curated tail entry can be the SAME post as a refreshed top-N entry (the feed window
    # overlaps the deck's history) — blank it so the post never shows twice.
    deduped = []
    for j in range(n, len(entries)):
        url = entry_url(entries[j], rid_target)
        text = px.shape_text(entries[j]).lower()
        if (url and norm_url(url) in refreshed_urls) or any(t and t in text for t in refreshed_titles):
            clear_entry(entries[j])
            deduped.append(j)
    changed = any(r.get("ok") for r in refreshed) or bool(deduped)
    if changed:
        slide_file.write_text(dom.toxml(), encoding="utf-8")
        rels_file.write_text(rels_dom.toxml(), encoding="utf-8")
    return {"entries_found": len(entries), "posts_available": len(posts),
            "refreshed": refreshed, "deduped": deduped, "changed": changed}


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
        ok = sum(1 for r in s["refreshed"] if r.get("ok"))
        log(f"slide {n} ({s['product']}): refreshed {ok}/{s['entries_found']} entries "
            f"(posts available: {s['posts_available']})"
            + (f"; de-duped tail entries {s['deduped']}" if s.get("deduped") else ""))
        for r in s["refreshed"]:
            if r.get("ok"):
                log(f"    - {r['date']} :: {r['title'][:60]}")
    log(f"report -> {args.report}" + ("  [DRY-RUN, deck not written]" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
