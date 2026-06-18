#!/usr/bin/env python3
"""Deterministic editor for the upcoming-events slides:

  * Webinar slides (10 = Terraform, 14 = Vault): fixed grid of webinar "slots". Fill the slots in
    reading order (top-to-bottom, left-to-right) with the product's upcoming webinars, SOONEST first;
    clear any surplus slots; if the product has zero upcoming webinars, hide the whole slide.
  * Featured / in-person slides (18-22): one event per slide. Assign the soonest upcoming featured
    events across the slides in order; hide any slide left without an event.

Each slot/slide keeps its exact formatting — only the text inside existing runs changes and hyperlink
targets are repointed via FRESH relationships (shared-rId safe). Multi-bullet descriptions collapse to
the single sentence from `data/events.json`. Hiding sets `show="0"` on the slide root (never deletes).

Runs AFTER the deck-data agent (which fills the description/location prose into events.json). Operates
in place on a working copy; refuses to touch the original. Writes `data/event-edit-report.json`.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from xml.dom import minidom

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.common import DATA_DIR, load_config, log  # noqa: E402
from lib import pptx_edit as px  # noqa: E402

FEATURED_TITLE = "Title 1"
FEATURED_DATE = "Subtitle 2"
FEATURED_DESC = "Content Placeholder 4"


# --------------------------------------------------------------------------- slot helpers ---------
def reading_order(slots: list) -> list:
    """Top-to-bottom, left-to-right. Round y to a row band so a near-equal row sorts by x."""
    def key(sp):
        x, y = px.shape_xy(sp)
        return (round(y or 0), x or 0)
    return sorted(slots, key=key)


def collapse_into_first_run(p, text: str) -> bool:
    runs = px.para_runs(p)
    if not runs:
        return False
    px.set_run_text(runs[0], text)
    for r in runs[1:]:
        px.remove_node(r)
    return True


def clear_txbody(sp) -> None:
    """Reduce a shape to a single empty paragraph (matches a hand-cleared slot)."""
    bodies = sp.getElementsByTagName("p:txBody")
    if not bodies:
        return
    body = bodies[0]
    for p in list(body.getElementsByTagName("a:p")):
        px.remove_node(p)
    doc = sp.ownerDocument
    p = doc.createElement("a:p")
    epr = doc.createElement("a:endParaRPr")
    epr.setAttribute("lang", "en-US")
    p.appendChild(epr)
    body.appendChild(p)


def fill_webinar_slot(sp, rels_dom, w: dict) -> bool:
    paras = px.paragraphs(sp)
    if len(paras) < 2:
        return False
    runs0 = px.para_runs(paras[0])
    see_idx = next((i for i, r in enumerate(runs0) if px.run_hyperlink(r) is not None), None)
    if see_idx is None or see_idx == 0:
        return False
    px.set_run_text(runs0[0], f"{w['display_date'].upper()} | WEBINAR | ")
    for r in runs0[1:see_idx]:
        px.remove_node(r)
    px.run_hyperlink(runs0[see_idx]).setAttribute("r:id", px.add_rel(rels_dom, w["url"]))
    collapse_into_first_run(paras[1], w["title"])
    desc = (w.get("description") or "").strip()
    if len(paras) >= 3:
        if desc and collapse_into_first_run(paras[2], desc):
            for p in paras[3:]:
                px.remove_node(p)
        else:
            for p in paras[2:]:
                px.remove_node(p)
    return True


# --------------------------------------------------------------------------- slide editors --------
def edit_webinar_slide(slide_file: Path, rels_file: Path, webinars: list[dict]) -> dict:
    dom = minidom.parse(str(slide_file))
    rels_dom, _ = px.load_rels(rels_file)
    slots = reading_order([sp for sp in dom.getElementsByTagName("p:sp")
                           if px.shape_name(sp) == "Google Shape;397;p78"])
    ordered = sorted(webinars, key=lambda w: w["date"])  # soonest first
    filled, cleared = [], 0
    for i, sp in enumerate(slots):
        if i < len(ordered) and fill_webinar_slot(sp, rels_dom, ordered[i]):
            filled.append(ordered[i]["title"])
        else:
            clear_txbody(sp)
            cleared += 1
    hidden = len(ordered) == 0
    if hidden:
        dom.documentElement.setAttribute("show", "0")
    slide_file.write_text(dom.toxml(), encoding="utf-8")
    rels_file.write_text(rels_dom.toxml(), encoding="utf-8")
    return {"slots": len(slots), "filled": filled, "cleared": cleared, "hidden": hidden,
            "available": len(ordered)}


def edit_featured_slide(slide_file: Path, rels_file: Path, event: dict | None) -> dict:
    dom = minidom.parse(str(slide_file))
    if event is None:
        dom.documentElement.setAttribute("show", "0")
        slide_file.write_text(dom.toxml(), encoding="utf-8")
        return {"hidden": True, "title": None}
    rels_dom, _ = px.load_rels(rels_file)
    by_name = {}
    for sp in dom.getElementsByTagName("p:sp"):
        by_name.setdefault(px.shape_name(sp), sp)

    title_sp = by_name.get(FEATURED_TITLE)
    if title_sp and px.paragraphs(title_sp):
        collapse_into_first_run(px.paragraphs(title_sp)[0], event["title"])

    date_sp = by_name.get(FEATURED_DATE)
    if date_sp and px.paragraphs(date_sp):
        p0 = px.paragraphs(date_sp)[0]
        runs = px.para_runs(p0)
        link_idx = next((i for i, r in enumerate(runs) if px.run_hyperlink(r) is not None), None)
        if link_idx is not None and link_idx > 0:
            px.set_run_text(runs[0], f"Event | {event['display_date']} | ")
            for r in runs[1:link_idx]:
                px.remove_node(r)
            px.run_hyperlink(runs[link_idx]).setAttribute("r:id", px.add_rel(rels_dom, event["url"]))

    desc_sp = by_name.get(FEATURED_DESC)
    desc = (event.get("description") or "").strip()
    if desc_sp and desc:
        paras = px.paragraphs(desc_sp)
        if paras and collapse_into_first_run(paras[0], desc):
            for p in paras[1:]:
                px.remove_node(p)

    slide_file.write_text(dom.toxml(), encoding="utf-8")
    rels_file.write_text(rels_dom.toxml(), encoding="utf-8")
    return {"hidden": False, "title": event["title"], "date": event["display_date"]}


def slide_paths(root: Path, n: int) -> tuple[Path, Path]:
    sf = root / "ppt" / "slides" / f"slide{n}.xml"
    rf = root / "ppt" / "slides" / "_rels" / f"slide{n}.xml.rels"
    return sf, rf


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("deck")
    ap.add_argument("--report", default=str(DATA_DIR / "event-edit-report.json"))
    ap.add_argument("--original", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    deck = Path(args.deck).resolve()
    if not deck.exists():
        raise SystemExit(f"deck not found: {deck}")
    if args.original and Path(args.original).resolve() == deck:
        raise SystemExit("refusing to edit the ORIGINAL deck in place")

    slide_map = load_config("slide-map.yaml")
    events = json.loads((DATA_DIR / "events.json").read_text(encoding="utf-8"))
    webinars = events.get("webinars", {})
    featured = sorted(events.get("featured", []), key=lambda e: e["date"])  # soonest first

    tmp = Path(tempfile.mkdtemp(prefix="eventedit-"))
    report = {"slides": {}, "any_change": False}
    try:
        px.unpack(deck, tmp)
        for entry in slide_map.get("slides", []):
            etype = entry.get("type")
            if etype == "webinars":
                n = entry["n"]
                sf, rf = slide_paths(tmp, n)
                if not sf.exists():
                    continue
                rep = edit_webinar_slide(sf, rf, webinars.get(entry["product"], []))
                rep["product"] = entry["product"]
                report["slides"][str(n)] = rep
                report["any_change"] = True
            elif etype == "in_person_event":
                ns = entry["n"]
                ns = ns if isinstance(ns, list) else [ns]
                for i, n in enumerate(ns):
                    sf, rf = slide_paths(tmp, n)
                    if not sf.exists():
                        continue
                    ev = featured[i] if i < len(featured) else None
                    report["slides"][str(n)] = edit_featured_slide(sf, rf, ev)
                    report["any_change"] = True
        if report["any_change"] and not args.dry_run:
            out = px.pack(tmp, deck)
            if out.returncode != 0:
                log(f"pack failed: {out.stderr[-600:]}")
                return 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    for n, s in sorted(report["slides"].items(), key=lambda kv: int(kv[0])):
        if "slots" in s:
            log(f"slide {n} ({s.get('product')}): filled {len(s['filled'])}/{s['slots']} slots, "
                f"cleared {s['cleared']}" + ("  [HIDDEN: no webinars]" if s["hidden"] else ""))
            for t in s["filled"]:
                log(f"    - {t[:60]}")
        else:
            if s["hidden"]:
                log(f"slide {n}: HIDDEN (no featured event)")
            else:
                log(f"slide {n}: {s['date']} :: {s['title'][:55]}")
    log(f"report -> {args.report}" + ("  [DRY-RUN, deck not written]" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
