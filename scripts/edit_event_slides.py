#!/usr/bin/env python3
"""Deterministic editor for the upcoming-events slides:

  * Webinar slides (10 = Terraform, 14 = Vault): fixed grid of webinar "slots". Fill the slots in
    reading order (top-to-bottom, left-to-right) with the product's upcoming webinars, SOONEST first;
    clear any surplus slots; if the product has zero upcoming webinars, hide the whole slide.
  * Featured / in-person slides (18-22): one event per slide. Assign the soonest upcoming featured
    events across the slides in order; hide any slide left without an event.

Each slot/slide keeps its exact formatting — only the text inside existing runs changes and hyperlink
targets are repointed via FRESH relationships (shared-rId safe). Featured-event descriptions (18-22)
render as 2-3 bullets from `description_points` (falling back to the single `description`) by reusing
the placeholder's own paragraph templates; long titles are switched to shrink-to-fit so they no longer
overflow the Register link. Each event's photo is swapped for its captured leadspace screenshot (the
HashiCorp gradient hero): the event's OWN per-page capture (the city-specific IBM leadspace) wins where
available, otherwise an on-brand HashiCorp SERIES hero (from the manifest `fallbacks`, matched by event
title) is used — so e.g. the Hands-On Lab shows the Agentic Runtime Security Exchange landing-page hero.
The screenshot is added as fresh media with a fresh image rel and the old per-event crop is reset so the
full-width gradient shows; unrelated events with no capture keep their existing photo. Hiding sets
`show="0"` on the slide root (never deletes).

Runs AFTER the deck-data agent (which fills the description/location prose into events.json). Operates
in place on a working copy; refuses to touch the original. Writes `data/event-edit-report.json`.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from xml.dom import minidom

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.common import DATA_DIR, load_config, log  # noqa: E402
from lib import pptx_edit as px  # noqa: E402

FEATURED_TITLE = "Title 1"
FEATURED_DATE = "Subtitle 2"
FEATURED_DESC = "Content Placeholder 4"
WEBINAR_SHAPE = "Google Shape;397;p78"


def slot_date_label(iso: str) -> str:
    """ISO date -> 'JUN 19, 2026' (the webinar-slot date style)."""
    try:
        return datetime.fromisoformat(iso).strftime("%b %-d, %Y").upper()
    except ValueError:
        return (iso or "").upper()


def _entry(src: dict, kind: str) -> dict:
    """Normalize a webinar slot or featured event into a uniform slot-fill entry."""
    return {"date": src.get("date", ""), "date_label": slot_date_label(src.get("date", "")),
            "kind": (kind or "WEBINAR").upper(), "title": src.get("title", ""),
            "url": src.get("url", ""), "description": (src.get("description") or "").strip()}


def _event_identity(e: dict) -> tuple:
    """Stable per-event identity for de-duplication — title+date, NOT url. Several distinct events
    can share a generic landing-page url (e.g. AWS Summit DC / Mexico City / Zurich all link to
    aws.amazon.com/events/summits/), so keying on url would wrongly collapse them into one and shrink
    the backfill pool below what's needed to fill every slot."""
    return (str(e.get("title", "")).strip().lower(), str(e.get("date", "")))


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


def fill_description(desc_sp, points: list[str], max_points: int = 3) -> int:
    """Render 2-3 description bullets by reusing the placeholder's EXISTING paragraph templates
    (so each point keeps the slide's own bullet/indent/font). One point per paragraph; if there are
    more points than template paragraphs, the last paragraph is cloned to hold them; surplus template
    paragraphs are removed. Returns how many points were written."""
    points = [p.strip() for p in (points or []) if p and p.strip()][:max_points]
    paras = px.paragraphs(desc_sp)
    if not paras or not points:
        return 0
    while len(paras) < len(points):  # grow by cloning the last bullet template
        clone = paras[-1].cloneNode(True)
        paras[-1].parentNode.appendChild(clone)
        paras.append(clone)
    n = len(points)
    for i in range(n):
        collapse_into_first_run(paras[i], points[i])
    for p in paras[n:]:
        px.remove_node(p)
    return n


def fit_title(title_sp, text: str) -> bool:
    """Stop a long featured-event title from overflowing the Register link below it.

    The title box uses <a:spAutoFit/> (the SHAPE grows to fit text), so a title that wraps to a
    second line expands the box down over the 'Subtitle 2' link. Swap to <a:normAutofit/> (fixed
    box, shrink text) with a length-based fontScale so long titles render on one line. Short titles
    are left untouched. Run formatting (size/colour/font) is never altered — only the box autofit."""
    bodies = title_sp.getElementsByTagName("a:bodyPr")
    if not bodies:
        return False
    body = bodies[0]
    n = len(text or "")
    if n <= 40:  # comfortably fits one line at full size — leave the original spAutoFit
        return False
    scale = "90000" if n <= 48 else "80000" if n <= 56 else "70000"
    for tag in ("a:spAutoFit", "a:normAutofit", "a:noAutofit"):
        for el in list(body.getElementsByTagName(tag)):
            px.remove_node(el)
    naf = title_sp.ownerDocument.createElement("a:normAutofit")
    naf.setAttribute("fontScale", scale)
    body.appendChild(naf)
    return True


def _pic_geom(pic):
    x = pic.getElementsByTagName("a:xfrm")
    if not x:
        return None
    ext = x[0].getElementsByTagName("a:ext")
    if not ext:
        return None
    return (int(ext[0].getAttribute("cx")), int(ext[0].getAttribute("cy")))


def find_hero_pic(dom):
    """Pick the slide's event-photo picture. The template's event hero is an explicit 'Picture
    Placeholder' on most featured slides, so prefer that (excluding the full-bleed background = the
    single largest pic). When no placeholder exists, fall back to the largest remaining picture that
    isn't a tiny logo (>1% of the background). Returns the <p:pic> element or None (photo left as-is)."""
    pics = []
    for pic in dom.getElementsByTagName("p:pic"):
        g = _pic_geom(pic)
        if not g:
            continue
        name = pic.getElementsByTagName("p:cNvPr")[0].getAttribute("name")
        pics.append((g[0] * g[1], name, pic))
    if len(pics) < 2:
        return None
    pics.sort(key=lambda t: t[0], reverse=True)
    bg_area = pics[0][0]
    rest = pics[1:]  # exclude the full-bleed background (single largest)
    placeholders = [t for t in rest if "placeholder" in t[1].lower()]
    if placeholders:
        return max(placeholders, key=lambda t: t[0])[2]
    cands = [t for t in rest if t[0] > 0.01 * bg_area]
    if not cands:
        return None
    return max(cands, key=lambda t: t[0])[2]


def pick_event_image(event: dict, manifest: dict, fallbacks: list[dict]) -> dict | None:
    """Choose the image record for an event: prefer its OWN per-page capture (the city-specific IBM
    leadspace), else fall back to an on-brand HashiCorp series hero whose `match` matches the event
    title. Returns a record with status=='ok' and a `file`, or None to keep the existing slide photo."""
    own = manifest.get(event.get("url", ""))
    if own and own.get("status") == "ok" and own.get("file"):
        return own
    title = event.get("title", "")
    for fb in fallbacks:
        if (fb.get("status") == "ok" and fb.get("file") and fb.get("match")
                and re.search(fb["match"], title, re.I)):
            return fb
    return None


def swap_event_image(dom, rels_dom, root: Path, slide_n: int, img_path: Path):
    """Replace the slide's event-photo with the captured leadspace screenshot by adding the image as
    a FRESH media file + relationship and repointing the picture's <a:blip r:embed> (never mutating a
    shared image rel or overwriting existing media). Returns info dict, or None if no suitable pic."""
    pic = find_hero_pic(dom)
    if pic is None:
        return None
    blips = pic.getElementsByTagName("a:blip")
    if not blips:
        return None
    media = root / "ppt" / "media"
    media.mkdir(parents=True, exist_ok=True)
    newname = f"image_evt_slide{slide_n}{img_path.suffix.lower() or '.png'}"
    shutil.copy(img_path, media / newname)
    rid = px.add_image_rel(rels_dom, f"../media/{newname}")
    blips[0].setAttribute("r:embed", rid)
    # The old per-event crop (a:srcRect) was tuned to the previous photo; reset it so the captured
    # leadspace shows full-width (the gradient background must not be cropped at the sides).
    for sr in pic.getElementsByTagName("a:srcRect"):
        sr.parentNode.removeChild(sr)
    name = pic.getElementsByTagName("p:cNvPr")[0].getAttribute("name")
    return {"shape": name, "media": newname, "embed": rid}


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


def fill_webinar_slot(sp, rels_dom, e: dict) -> bool:
    paras = px.paragraphs(sp)
    if len(paras) < 2:
        return False
    runs0 = px.para_runs(paras[0])
    see_idx = next((i for i, r in enumerate(runs0) if px.run_hyperlink(r) is not None), None)
    if see_idx is None or see_idx == 0:
        return False
    px.set_run_text(runs0[0], f"{e['date_label']} | {e['kind']} | ")
    for r in runs0[1:see_idx]:
        px.remove_node(r)
    px.run_hyperlink(runs0[see_idx]).setAttribute("r:id", px.add_rel(rels_dom, e["url"]))
    collapse_into_first_run(paras[1], e["title"])
    desc = (e.get("description") or "").strip()
    if len(paras) >= 3:
        if desc and collapse_into_first_run(paras[2], desc):
            for p in paras[3:]:
                px.remove_node(p)
        else:
            for p in paras[2:]:
                px.remove_node(p)
    return True


# --------------------------------------------------------------------------- slide editors --------
def webinar_slots(dom) -> list:
    return reading_order([sp for sp in dom.getElementsByTagName("p:sp")
                          if px.shape_name(sp) == WEBINAR_SHAPE])


def count_slots(slide_file: Path) -> int:
    return len(webinar_slots(minidom.parse(str(slide_file))))


def edit_webinar_slide(slide_file: Path, rels_file: Path, fill: list[dict]) -> dict:
    """Fill each slot from the precomputed `fill` list (primary product webinars + backfills, already
    allocated and de-duplicated globally). Only clears a slot if the pool ran dry (kept as a guard)."""
    dom = minidom.parse(str(slide_file))
    rels_dom, _ = px.load_rels(rels_file)
    slots = webinar_slots(dom)
    filled, backfilled, cleared = [], [], 0
    for i, sp in enumerate(slots):
        if i < len(fill) and fill_webinar_slot(sp, rels_dom, fill[i]):
            filled.append(fill[i]["title"])
            if fill[i].get("backfill"):
                backfilled.append(f"{fill[i]['kind']}: {fill[i]['title'][:50]}")
        else:
            clear_txbody(sp)
            cleared += 1
    hidden = len(filled) == 0
    if hidden:
        dom.documentElement.setAttribute("show", "0")
    pruned = px.prune_orphan_hyperlinks(dom, rels_dom)
    slide_file.write_text(dom.toxml(), encoding="utf-8")
    rels_file.write_text(rels_dom.toxml(), encoding="utf-8")
    return {"slots": len(slots), "filled": filled, "backfilled": backfilled, "cleared": cleared,
            "hidden": hidden, "available": len(fill), "pruned_rels": pruned}


def edit_featured_slide(slide_file: Path, rels_file: Path, event: dict | None,
                        root: Path | None = None, image_rec: dict | None = None,
                        slide_n: int | None = None) -> dict:
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
    title_fit = False
    if title_sp and px.paragraphs(title_sp):
        collapse_into_first_run(px.paragraphs(title_sp)[0], event["title"])
        title_fit = fit_title(title_sp, event["title"])

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
    points = event.get("description_points") or []
    if not points:
        single = (event.get("description") or "").strip()
        points = [single] if single else []
    desc_written = fill_description(desc_sp, points) if desc_sp else 0

    image_swapped = None
    if root is not None and slide_n is not None and image_rec and image_rec.get("status") == "ok":
        img_path = (DATA_DIR.parent / image_rec["file"]).resolve()
        if img_path.exists():
            image_swapped = swap_event_image(dom, rels_dom, root, slide_n, img_path)

    pruned = px.prune_orphan_hyperlinks(dom, rels_dom)
    slide_file.write_text(dom.toxml(), encoding="utf-8")
    rels_file.write_text(rels_dom.toxml(), encoding="utf-8")
    return {"hidden": False, "title": event["title"], "date": event["display_date"],
            "desc_points": desc_written, "title_fit": title_fit,
            "image_swapped": bool(image_swapped), "pruned_rels": pruned}


def slide_paths(root: Path, n: int) -> tuple[Path, Path]:
    sf = root / "ppt" / "slides" / f"slide{n}.xml"
    rf = root / "ppt" / "slides" / "_rels" / f"slide{n}.xml.rels"
    return sf, rf


def allocate_webinars(webinar_slides: list[tuple], webinars: dict, featured: list[dict],
                      reserved_featured_urls: set) -> dict:
    """Globally allocate slot-fill entries so no event repeats across slides and no in-scope slot is
    left blank while events remain. Each slide first takes its OWN product's webinars (soonest-first),
    then empty slots are backfilled from a shared pool of cross-product/other webinars + featured events
    NOT already shown on the in-person slides (18-22) — mirroring the hand-authored deck, which always
    filled every slot with cert/cross-product/regional sessions."""
    used = set()
    plan = {}
    # Pass 1 — primary product webinars.
    for n, product, slots in webinar_slides:
        chosen = []
        for w in sorted(webinars.get(product, []), key=lambda x: x.get("date", "")):
            if len(chosen) >= slots:
                break
            if _event_identity(w) in used:
                continue
            chosen.append(_entry(w, "WEBINAR"))
            used.add(_event_identity(w))
        plan[n] = chosen
    # Shared backfill pool — every remaining webinar (any bucket) + non-reserved featured.
    pool = []
    for bucket in webinars.values():
        for w in bucket:
            if _event_identity(w) not in used:
                pool.append(_entry(w, "WEBINAR"))
    for f in featured:
        if _event_identity(f) not in used and f["url"] not in reserved_featured_urls:
            ent = _entry(f, f.get("type", "EVENT"))
            ent["backfill"] = True
            pool.append(ent)
    seen, dedup = set(), []
    for e in sorted(pool, key=lambda x: x["date"]):
        if _event_identity(e) in seen:
            continue
        seen.add(_event_identity(e))
        dedup.append(e)
    # Pass 2 — distribute the pool across empty slots in slide order.
    pi = 0
    for n, _product, slots in webinar_slides:
        while len(plan[n]) < slots and pi < len(dedup):
            e = dedup[pi]
            pi += 1
            if _event_identity(e) in used:
                continue
            e.setdefault("backfill", True)
            plan[n].append(e)
            used.add(_event_identity(e))
        plan[n].sort(key=lambda x: x["date"])  # soonest-first reading order, like the hand-authored deck
    return plan


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

    # Optional Playwright leadspace screenshots (keyed by event URL) + on-brand series fallbacks.
    # Missing/failed → photo kept.
    image_manifest = {}
    image_fallbacks: list[dict] = []
    img_file = DATA_DIR / "event-images.json"
    if img_file.exists():
        try:
            _imgs = json.loads(img_file.read_text(encoding="utf-8"))
            image_manifest = _imgs.get("images", {})
            image_fallbacks = list((_imgs.get("fallbacks") or {}).values())
        except (ValueError, OSError):
            image_manifest = {}

    # Featured events shown on the in-person slides (18-22) are reserved — excluded from webinar
    # backfill so an event never appears twice in the deck.
    inperson_ns = []
    for entry in slide_map.get("slides", []):
        if entry.get("type") == "in_person_event":
            ns = entry["n"]
            inperson_ns.extend(ns if isinstance(ns, list) else [ns])
    reserved_featured_urls = {f["url"] for f in featured[:len(inperson_ns)]}

    tmp = Path(tempfile.mkdtemp(prefix="eventedit-"))
    report = {"slides": {}, "any_change": False}
    try:
        px.unpack(deck, tmp)
        # Collect webinar slides + their slot counts up front so allocation can span all of them.
        webinar_slides = []
        for entry in slide_map.get("slides", []):
            if entry.get("type") != "webinars":
                continue
            n = entry["n"]
            sf, _rf = slide_paths(tmp, n)
            if sf.exists():
                webinar_slides.append((n, entry["product"], count_slots(sf)))
        plan = allocate_webinars(webinar_slides, webinars, featured, reserved_featured_urls)

        for entry in slide_map.get("slides", []):
            etype = entry.get("type")
            if etype == "webinars":
                n = entry["n"]
                sf, rf = slide_paths(tmp, n)
                if not sf.exists():
                    continue
                rep = edit_webinar_slide(sf, rf, plan.get(n, []))
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
                    img_rec = pick_event_image(ev, image_manifest, image_fallbacks) if ev else None
                    report["slides"][str(n)] = edit_featured_slide(
                        sf, rf, ev, root=tmp, image_rec=img_rec, slide_n=n)
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
            extra = f", backfilled {len(s['backfilled'])}" if s.get("backfilled") else ""
            log(f"slide {n} ({s.get('product')}): filled {len(s['filled'])}/{s['slots']} slots, "
                f"cleared {s['cleared']}{extra}" + ("  [HIDDEN: no webinars]" if s["hidden"] else ""))
            for t in s["filled"]:
                log(f"    - {t[:60]}")
        else:
            if s["hidden"]:
                log(f"slide {n}: HIDDEN (no featured event)")
            else:
                img = "  [photo updated]" if s.get("image_swapped") else ""
                log(f"slide {n}: {s['date']} :: {s['title'][:55]}{img}")
    log(f"report -> {args.report}" + ("  [DRY-RUN, deck not written]" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
