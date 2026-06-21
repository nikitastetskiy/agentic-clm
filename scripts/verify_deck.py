#!/usr/bin/env python3
"""Deterministic post-build verifier for the cadence deck.

This is the "stop doing it by hand" automation: it machine-checks a produced deck the CORRECT way so
that neither a human nor the AI reviewer has to eyeball slide XML (two mistakes this replaces: the
reviewer's false hide-FAIL from checking presentation.xml instead of the slide root, and a human
mis-reading truncated column text). It runs in run.sh after the build (a cheap, exact gate) and the
AI reviewer reads its report and focuses on the things only judgment can assess (style nuance, prose).

Checks (all deterministic, no AI):
  1. Integrity      — output is a valid deck and (vs --original) has the SAME slide count (nothing deleted).
  2. Hide audit     — which slides are hidden, via <p:sld ... show="0"> on the SLIDE ROOT (the real
                      PowerPoint "Hide Slide" flag), mapped to display positions. Flags event/webinar/
                      featured slides that are still VISIBLE while their events.json status is unavailable
                      (the actual risk: shipping past-dated events).
  3. Style          — runs the hashicorp-deck-style style_check.py on the in-scope slides.
  4. Release cols   — for each release slide, reports each column's header + entry count and asserts the
                      latest (rightmost) column carries products.<product>.latest.version from releases.json.
  5. Release accuracy — date-keyed: every shown entry "vX.Y.Z (YYYY-MM-DD)" must match releases.json for
                      that exact DATE (the authoritative key). Catches mislabels the col3-only check missed
                      (e.g. an entry dated 2026-05-27 typed as 1.15.4 when the API says that date is 1.15.5).
  6. Blog coverage   — the newest blogs.json posts appear on slides 9/13, in order, with no duplicates.
  7. Event coverage  — webinar slots / featured slides carry known events; empty-supply slides are hidden.
  8. No empty box    — LOOP-HARDENING: a VISIBLE webinar slide has every slot filled and a blog slide keeps
                      its full original entry count (catches a cleared surplus slot / blanked blog entry).
  9. Release gap     — LOOP-HARDENING: no same-month same-line release is skipped "as historical", for
                      EVERY month column the slide displays (full historical window, not just the latest
                      — e.g. a missing May patch is caught as well as a missing June one).
 10. Photo consistency — LOOP-HARDENING: each featured-event slide uses its OWN per-city leadspace
                      hero capture (clean title+gradient), or the on-brand series hero as a fallback
                      when the event page has no leadspace — never mixed registration buttons/blank-white.

Usage:
    python scripts/verify_deck.py "output/<deck>.pptx" [--original "<original>.pptx"] [--json]

Exit code 0 if every check PASSes, 1 otherwise. Pass --json for a machine-readable summary on stdout.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.dom import minidom

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.common import DATA_DIR, REPO_ROOT, load_config, log  # noqa: E402

UNPACK = REPO_ROOT / ".github/skills/pptx/scripts/office/unpack.py"
STYLE_CHECK = REPO_ROOT / ".github/skills/hashicorp-deck-style/scripts/style_check.py"
EMU_PER_IN = 914400
COL_BOUNDS = [(0.0, 2.0), (2.0, 5.0), (5.0, 12.0)]  # x-buckets for the 3 release columns
HEADER_Y_MAX = 1.5  # y below this on a column = month header; above = release entry
SLD_ROOT_RE = re.compile(r"<p:sld\b[^>]*>")
VERSION_RE = re.compile(r"v?(\d+\.\d+\.\d+)")
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
# slide-map product name -> releases.json location of that product's {latest, recent[]}
PRODUCT_KEY = {"terraform": ("products", "terraform"), "vault": ("products", "vault"),
               "terraform-enterprise": ("terraform_enterprise", None)}


def _unpack(deck: Path) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="verify-"))
    subprocess.run([sys.executable, str(UNPACK), str(deck), str(tmp)], check=True, capture_output=True)
    return tmp


def _text(node) -> str:
    return "".join(t.firstChild.data for t in node.getElementsByTagName("a:t") if t.firstChild)


def slide_order(unpacked: Path) -> list[Path]:
    """Resolve display order (1-based) -> slideN.xml file, via presentation.xml + rels."""
    pres = unpacked / "ppt" / "presentation.xml"
    rels = unpacked / "ppt" / "_rels" / "presentation.xml.rels"
    rid_to_target = {
        r.getAttribute("Id"): r.getAttribute("Target")
        for r in minidom.parse(str(rels)).getElementsByTagName("Relationship")
    }
    order: list[Path] = []
    lst = minidom.parse(str(pres)).getElementsByTagName("p:sldIdLst")
    if not lst:
        return order
    for sld in lst[0].getElementsByTagName("p:sldId"):
        target = rid_to_target.get(sld.getAttribute("r:id"), "")
        order.append((unpacked / "ppt" / target.replace("../", "")).resolve())
    return order


def is_hidden(slide_file: Path) -> bool:
    """True iff the slide ROOT carries show="0" (PowerPoint's real Hide-Slide flag).

    The flag lives on <p:sld ... show="0">, which is NOT the first '>' in the file (that is the
    <?xml?> declaration) — so locate the slide-root tag explicitly.
    """
    m = SLD_ROOT_RE.search(slide_file.read_text(encoding="utf-8")[:2000])
    return bool(m and 'show="0"' in m.group(0))


def _round_in(v: str | None) -> float | None:
    try:
        return round(int(v) / EMU_PER_IN, 2)
    except (TypeError, ValueError):
        return None


def release_columns(slide_file: Path) -> dict[int, dict]:
    """Group release-slide shapes into 3 columns -> {header, version_entries[]}.

    A "version entry" is any paragraph containing a vX.Y.Z token (entries may be prefixed, e.g.
    "Terraform Enterprise v2.0.3 (...)"). The bare semver is stored for comparison.
    """
    doc = minidom.parse(str(slide_file))
    cols: dict[int, dict] = {i: {"header": None, "entries": []} for i in range(3)}
    for sp in doc.getElementsByTagName("p:sp"):
        off = sp.getElementsByTagName("a:off")
        if not off:
            continue
        x = _round_in(off[0].getAttribute("x"))
        y = _round_in(off[0].getAttribute("y"))
        if x is None or y is None or y < 1.2:
            continue
        col = next((i for i, (lo, hi) in enumerate(COL_BOUNDS) if lo <= x < hi), None)
        if col is None:
            continue
        for p in sp.getElementsByTagName("a:p"):
            txt = _text(p).strip()
            if not txt:
                continue
            m = VERSION_RE.search(txt)
            if m:
                cols[col]["entries"].append(m.group(1))
            elif y < HEADER_Y_MAX and cols[col]["header"] is None:
                cols[col]["header"] = txt
    return cols


def slide_dated_entries(slide_file: Path) -> list[tuple[str, str]]:
    """Every release entry on a slide as (semver, 'YYYY-MM-DD'), for date-keyed accuracy checks.

    A release entry paragraph looks like "v1.15.5 (2026-05-27)" or
    "Terraform Enterprise v2.0.3 (2026-06-17)" — one version + one date in the same paragraph.
    """
    doc = minidom.parse(str(slide_file))
    out: list[tuple[str, str]] = []
    for sp in doc.getElementsByTagName("p:sp"):
        off = sp.getElementsByTagName("a:off")
        if not off:
            continue
        y = _round_in(off[0].getAttribute("y"))
        if y is None or y < 1.2:
            continue
        for p in sp.getElementsByTagName("a:p"):
            txt = _text(p).strip()
            mv, md = VERSION_RE.search(txt), DATE_RE.search(txt)
            if mv and md:
                out.append((mv.group(1), md.group(1)))
    return out


def release_by_date(releases: dict) -> dict[str, dict[str, str]]:
    """Build {product_key: {'YYYY-MM-DD': version}} from releases.json latest + recent[]."""
    by_date: dict[str, dict[str, str]] = {}
    for product, (top, sub) in PRODUCT_KEY.items():
        node = releases.get(top, {})
        node = node.get(sub, {}) if sub else node
        dates: dict[str, str] = {}
        for rec in [node.get("latest")] + list(node.get("recent", [])):
            if rec and rec.get("date") and rec.get("version"):
                dates.setdefault(rec["date"], rec["version"])
        by_date[product] = dates
    return by_date


def _shape_name(sp) -> str:
    nv = sp.getElementsByTagName("p:cNvPr")
    return nv[0].getAttribute("name") if nv else ""


def _shape_y(sp) -> float | None:
    off = sp.getElementsByTagName("a:off")
    return _round_in(off[0].getAttribute("y")) if off else None


def shape_text_by_name(slide_file: Path, name: str) -> str | None:
    doc = minidom.parse(str(slide_file))
    for sp in doc.getElementsByTagName("p:sp"):
        if _shape_name(sp) == name:
            return _text(sp).strip()
    return None


def blog_entry_texts(slide_file: Path) -> list[str]:
    """Recent-blog entry shapes (a 'See here' hyperlink below the header), in document order."""
    doc = minidom.parse(str(slide_file))
    out: list[str] = []
    for sp in doc.getElementsByTagName("p:sp"):
        y = _shape_y(sp)
        if y is None or y < 1.3:
            continue
        if sp.getElementsByTagName("a:hlinkClick") and "See here" in _text(sp).replace("\xa0", " "):
            out.append(_text(sp).strip())
    return out


def webinar_slot_texts(slide_file: Path) -> list[str]:
    """Non-empty webinar slot shapes ('Google Shape;397;p78') on a webinar slide."""
    doc = minidom.parse(str(slide_file))
    return [t for sp in doc.getElementsByTagName("p:sp") if _shape_name(sp) == "Google Shape;397;p78"
            for t in [_text(sp).strip()] if t]


def webinar_slot_total(slide_file: Path) -> int:
    """Count ALL webinar slot shapes (empty + filled) — an emptied slot keeps its shape name."""
    doc = minidom.parse(str(slide_file))
    return sum(1 for sp in doc.getElementsByTagName("p:sp")
               if _shape_name(sp) == "Google Shape;397;p78")


def _pic_geom(pic) -> tuple[int, int] | None:
    x = pic.getElementsByTagName("a:xfrm")
    if not x:
        return None
    ext = x[0].getElementsByTagName("a:ext")
    if not ext:
        return None
    try:
        return (int(ext[0].getAttribute("cx")), int(ext[0].getAttribute("cy")))
    except ValueError:
        return None


def _hero_pic_embed(slide_file: Path) -> str | None:
    """Return the r:embed for the featured-event photo using the editor's hero-pic heuristic."""
    doc = minidom.parse(str(slide_file))
    pics = []
    for pic in doc.getElementsByTagName("p:pic"):
        geom = _pic_geom(pic)
        if not geom:
            continue
        cnv = pic.getElementsByTagName("p:cNvPr")
        name = cnv[0].getAttribute("name") if cnv else ""
        pics.append((geom[0] * geom[1], name, pic))
    if len(pics) < 2:
        return None
    pics.sort(key=lambda t: t[0], reverse=True)
    bg_area = pics[0][0]
    rest = pics[1:]
    placeholders = [t for t in rest if "placeholder" in t[1].lower()]
    candidates = placeholders or [t for t in rest if t[0] > 0.01 * bg_area]
    if not candidates:
        return None
    pic = max(candidates, key=lambda t: t[0])[2]
    blips = pic.getElementsByTagName("a:blip")
    return blips[0].getAttribute("r:embed") if blips else None


def _slide_rel_target(slide_file: Path, rid: str) -> Path | None:
    rels_file = slide_file.parent / "_rels" / f"{slide_file.name}.rels"
    if not rels_file.exists() or not rid:
        return None
    rels = minidom.parse(str(rels_file)).getElementsByTagName("Relationship")
    for rel in rels:
        if rel.getAttribute("Id") == rid:
            target = rel.getAttribute("Target")
            return (slide_file.parent / target).resolve()
    return None


def _sha256(path: Path | None) -> str | None:
    if not path or not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def release_month_window(releases: dict, product: str) -> list[dict]:
    """The same-line release history window for a product (used to detect a skipped same-month patch)."""
    top, sub = PRODUCT_KEY.get(product, (None, None))
    if top is None:
        return []
    node = releases.get(top, {})
    node = node.get(sub, {}) if sub else node
    return node.get("month_window", []) or []


def _load_json(name: str) -> dict:
    p = DATA_DIR / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _matching_series_fallback(event: dict, fallbacks: list[dict]) -> dict | None:
    title = event.get("title", "")
    for fb in fallbacks:
        if not (fb.get("status") == "ok" and fb.get("file") and fb.get("match")):
            continue
        try:
            if re.search(fb["match"], title, re.I):
                return fb
        except re.error:
            continue
    return None


def _count_slides(deck: Path) -> int:
    with zipfile.ZipFile(deck) as z:
        return sum(1 for n in z.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("deck")
    ap.add_argument("--original", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    deck = Path(args.deck).resolve()
    if not deck.exists():
        raise SystemExit(f"deck not found: {deck}")
    original = Path(args.original).resolve() if args.original else None
    cfg = load_config("slide-map.yaml")
    releases = _load_json("releases.json")
    events = _load_json("events.json")
    blogs = _load_json("blogs.json")
    event_images = _load_json("event-images.json")

    results: dict[str, dict] = {}
    unpacked = _unpack(deck)
    orig_unpacked = _unpack(original) if (original and original.exists()) else None
    try:
        order = slide_order(unpacked)
        total = len(order)

        # --- 1. Integrity ---------------------------------------------------------------
        integ = {"slides": total, "ok": True, "notes": []}
        if original and original.exists():
            o = _count_slides(original)
            integ["original_slides"] = o
            if o != total:
                integ["ok"] = False
                integ["notes"].append(f"slide count changed: original {o} -> output {total} (deletion/addition!)")
            if original == deck:
                integ["ok"] = False
                integ["notes"].append("output path == original path (original would be overwritten)")
        results["integrity"] = integ

        # --- 2. Hide audit --------------------------------------------------------------
        hidden = {i + 1 for i, f in enumerate(order) if is_hidden(f)}
        # event/webinar/featured slides that SHOULD be hidden when events are unavailable/empty
        event_types = {"webinars", "in_person_event"}
        event_positions: list[int] = []
        for entry in cfg.get("slides", []):
            if entry.get("type") in event_types:
                ns = entry["n"]
                event_positions += ns if isinstance(ns, list) else [ns]
        events_unavailable = events.get("status") == "unavailable" or not (
            events.get("webinars") or events.get("featured")
        )
        stale_visible = sorted(p for p in event_positions if events_unavailable and p not in hidden)
        hide = {
            "hidden_positions": sorted(hidden),
            "events_unavailable": events_unavailable,
            "event_slides": sorted(set(event_positions)),
            "stale_visible": stale_visible,
            "ok": not stale_visible,
            "notes": ([f"event slides VISIBLE despite unavailable events (past-dated risk): {stale_visible}"] if stale_visible else []),
        }
        results["hide_audit"] = hide

        # --- 3. Style -------------------------------------------------------------------
        in_scope = []
        for entry in cfg.get("slides", []):
            ns = entry["n"]
            in_scope += ns if isinstance(ns, list) else [ns]
        in_scope = [str(n) for n in sorted(set(in_scope))]
        proc = subprocess.run(
            [sys.executable, str(STYLE_CHECK), str(unpacked), *in_scope],
            capture_output=True, text=True,
        )
        results["style"] = {
            "ok": proc.returncode == 0,
            "exit": proc.returncode,
            "notes": ([proc.stdout.strip().splitlines()[-1]] if proc.returncode != 0 and proc.stdout else []),
        }

        # --- 4. Release columns ---------------------------------------------------------
        rel = {"slides": {}, "ok": True, "notes": []}
        prod_latest = {
            k: (v.get("latest") or {}).get("version")
            for k, v in releases.get("products", {}).items()
        }
        prod_latest["terraform-enterprise"] = (releases.get("terraform_enterprise", {}).get("latest") or {}).get("version")
        for entry in cfg.get("slides", []):
            if entry.get("type") != "release_updates":
                continue
            ns = entry["n"]
            for n in (ns if isinstance(ns, list) else [ns]):
                f = unpacked / "ppt" / "slides" / f"slide{n}.xml"
                if not f.exists():
                    continue
                cols = release_columns(f)
                product = entry.get("product")
                latest_v = prod_latest.get(product)
                col3 = cols[2]
                # No latest in data (e.g. TFE marked stale_source/unavailable) -> the slide is
                # correctly left unchanged; there is nothing to place, so treat it as satisfied.
                col3_has_latest = (latest_v in col3["entries"]) if latest_v else True
                summary = {
                    "headers": [cols[i]["header"] for i in range(3)],
                    "entry_counts": [len(cols[i]["entries"]) for i in range(3)],
                    "col3_entries": col3["entries"],
                    "expected_latest": latest_v,
                    "col3_has_latest": col3_has_latest,
                }
                rel["slides"][str(n)] = summary
                if latest_v and not col3_has_latest:
                    rel["ok"] = False
                    rel["notes"].append(f"slide {n} ({product}): latest col missing {latest_v} (has {col3['entries']})")
        results["release_columns"] = rel

        # --- 5. Release accuracy (date-keyed) -------------------------------------------
        # Catches the mislabel class (e.g. an entry dated 2026-05-27 typed as 1.15.4 when the API
        # says that date is 1.15.5). The exact release DATE is the authoritative key; the printed
        # version must agree with releases.json for that date. This is the guard that the original
        # col3-only check lacked — it would have caught the v1.15.5 typo on slide 7.
        acc = {"slides": {}, "ok": True, "notes": []}
        by_date = release_by_date(releases)
        for entry in cfg.get("slides", []):
            if entry.get("type") != "release_updates":
                continue
            product = entry.get("product")
            dates = by_date.get(product, {})
            if not dates:
                continue
            ns = entry["n"]
            for n in (ns if isinstance(ns, list) else [ns]):
                f = unpacked / "ppt" / "slides" / f"slide{n}.xml"
                if not f.exists():
                    continue
                mismatches = []
                for ver, date in slide_dated_entries(f):
                    expected = dates.get(date)
                    if expected and expected != ver:
                        mismatches.append(f"{date}: shows {ver}, data says {expected}")
                acc["slides"][str(n)] = mismatches
                if mismatches:
                    acc["ok"] = False
                    for m in mismatches:
                        acc["notes"].append(f"slide {n} ({product}): {m}")
        results["release_accuracy"] = acc

        # --- 6. Blog coverage -----------------------------------------------------------
        # Insert-and-shift semantics: the genuinely-NEW posts (in blogs.json but not already on the
        # original slide) must appear at the TOP of the slide, newest-first; already-shown posts simply
        # stay (and may shift off the bottom). No post may appear twice. Catches a stale/mis-ordered
        # blog refresh without falsely demanding already-present posts sit at fixed top-N positions.
        blogcov = {"slides": {}, "ok": True, "notes": []}
        for entry in cfg.get("slides", []):
            if entry.get("type") != "recent_blogs":
                continue
            product = entry.get("product")
            recent = (blogs.get("products", {}).get(product, {}) or {}).get("recent", [])
            ns = entry["n"]
            for n in (ns if isinstance(ns, list) else [ns]):
                f = unpacked / "ppt" / "slides" / f"slide{n}.xml"
                if not f.exists() or not recent:
                    continue
                texts = blog_entry_texts(f)
                orig_texts = []
                if orig_unpacked is not None:
                    of = orig_unpacked / "ppt" / "slides" / f"slide{n}.xml"
                    if of.exists():
                        orig_texts = blog_entry_texts(of)
                orig_blob = " || ".join(orig_texts)
                fresh = [p for p in recent if p["title"] not in orig_blob]  # newest-first already
                misses = []
                for i, post in enumerate(fresh):
                    if i >= len(texts):
                        misses.append(f"new post not placed: '{post['title'][:40]}'")
                    elif post["title"] not in texts[i]:
                        misses.append(f"top entry {i+1}: deck has '{texts[i][:45]}' "
                                      f"expected new post '{post['title'][:40]}'")
                norm = [" ".join(t.split()).lower() for t in texts]
                for i, t in enumerate(norm):
                    if t and t in norm[:i]:
                        misses.append(f"duplicate blog entry: '{texts[i][:45]}'")
                blogcov["slides"][str(n)] = misses
                if misses:
                    blogcov["ok"] = False
                    for m in misses:
                        blogcov["notes"].append(f"slide {n} ({product}): {m}")
        results["blog_coverage"] = blogcov

        # --- 7. Event coverage ----------------------------------------------------------
        # Webinar slides (10,14): #filled slots == min(slots, available webinars), every filled slot
        # carries a known webinar title, and a product with zero webinars hides the slide.
        # Featured slides (18-22): the soonest featured events appear (title match), one per slide,
        # and any slide past the supply is hidden (show="0", never deleted).
        evcov = {"slides": {}, "ok": True, "notes": []}
        webinars = events.get("webinars", {})
        featured = sorted(events.get("featured", []), key=lambda e: e.get("date", ""))
        # Slots may be BACKFILLED with cross-product/other webinars or featured events (exactly as the
        # hand-authored deck did), so a filled slot is valid if its title matches ANY known event, not
        # just the slide's own product bucket.
        all_event_titles = {w["title"] for bucket in webinars.values() for w in bucket}
        all_event_titles |= {f["title"] for f in featured}
        for entry in cfg.get("slides", []):
            etype = entry.get("type")
            if etype == "webinars":
                n = entry["n"]
                f = unpacked / "ppt" / "slides" / f"slide{n}.xml"
                if not f.exists():
                    continue
                avail = webinars.get(entry.get("product"), [])
                slots = webinar_slot_texts(f)
                notes = []
                if not avail and not all_event_titles:
                    if not is_hidden(f):
                        notes.append("no events but slide is VISIBLE (should be hidden)")
                else:
                    for t in slots:
                        if not any(wt and wt in t for wt in all_event_titles):
                            notes.append(f"slot text not in data: '{t[:45]}'")
                evcov["slides"][str(n)] = notes
                if notes:
                    evcov["ok"] = False
                    for m in notes:
                        evcov["notes"].append(f"slide {n} (webinars): {m}")
            elif etype == "in_person_event":
                ns = entry["n"]
                ns = ns if isinstance(ns, list) else [ns]
                for i, n in enumerate(ns):
                    f = unpacked / "ppt" / "slides" / f"slide{n}.xml"
                    if not f.exists():
                        continue
                    notes = []
                    if i < len(featured):
                        title = shape_text_by_name(f, "Title 1") or ""
                        if featured[i]["title"] not in title:
                            notes.append(f"title '{title[:45]}' != expected '{featured[i]['title'][:40]}'")
                    elif not is_hidden(f):
                        notes.append("no event for this slide but it is VISIBLE (should be hidden)")
                    evcov["slides"][str(n)] = notes
                    if notes:
                        evcov["ok"] = False
                        for m in notes:
                            evcov["notes"].append(f"slide {n} (featured): {m}")
        results["event_coverage"] = evcov

        # --- 8. No empty in-scope box (LOOP-HARDENING) ----------------------------------
        # The hand-authored deck NEVER leaves a webinar slot or blog entry blank. A blank box is a
        # silent defect the old checks accepted (a cleared surplus slot, a blanked duplicate blog
        # entry). This makes it a hard FAIL: every VISIBLE webinar slide must have all slots filled,
        # and every blog slide must keep its full original entry count (none blanked).
        emptybox = {"slides": {}, "ok": True, "notes": []}
        for entry in cfg.get("slides", []):
            etype = entry.get("type")
            if etype == "webinars":
                n = entry["n"]
                f = unpacked / "ppt" / "slides" / f"slide{n}.xml"
                if not f.exists() or is_hidden(f):
                    continue
                empties = webinar_slot_total(f) - len(webinar_slot_texts(f))
                note = [f"{empties} empty webinar slot(s)"] if empties > 0 else []
                emptybox["slides"][str(n)] = note
                if note:
                    emptybox["ok"] = False
                    emptybox["notes"].append(f"slide {n} (webinars): {note[0]}")
            elif etype == "recent_blogs":
                ns = entry["n"]
                for n in (ns if isinstance(ns, list) else [ns]):
                    f = unpacked / "ppt" / "slides" / f"slide{n}.xml"
                    if not f.exists():
                        continue
                    out_n = len(blog_entry_texts(f))
                    exp_n = out_n
                    if orig_unpacked is not None:
                        of = orig_unpacked / "ppt" / "slides" / f"slide{n}.xml"
                        if of.exists():
                            exp_n = len(blog_entry_texts(of))
                    note = ([f"{exp_n - out_n} blog entry box(es) blanked (have {out_n}, "
                             f"expected {exp_n})"] if out_n < exp_n else [])
                    emptybox["slides"][str(n)] = note
                    if note:
                        emptybox["ok"] = False
                        emptybox["notes"].append(f"slide {n} (blogs): {note[0]}")
        results["no_empty_box"] = emptybox

        # --- 9. Release version gap (LOOP-HARDENING) ------------------------------------
        # A same-month patch must never be skipped "as historical" — and this holds for EVERY month
        # column the slide displays (the rolling window's full historical context), not only the
        # latest. For each release slide and each month present in the product's month_window, every
        # same-major.minor-line release of a DISPLAYED month must appear in the slide's shown versions.
        # Months not shown on the slide (scrolled off the window / fallback slide) are skipped, and a
        # month with <2 releases is owned by the latest-column / release_columns checks. LTS backports
        # never trigger a false gap because month_window holds only the latest line.
        gap = {"slides": {}, "ok": True, "notes": []}
        for entry in cfg.get("slides", []):
            if entry.get("type") != "release_updates":
                continue
            product = entry.get("product")
            mw = release_month_window(releases, product)
            if not mw:
                continue
            months: dict[str, list[str]] = {}
            for r in mw:
                m, v = r.get("month_label"), r.get("version")
                if m and v and v not in months.setdefault(m, []):
                    months[m].append(v)
            ns = entry["n"]
            for n in (ns if isinstance(ns, list) else [ns]):
                f = unpacked / "ppt" / "slides" / f"slide{n}.xml"
                if not f.exists():
                    continue
                shown = {ver for ver, _d in slide_dated_entries(f)}
                missing_all: list[str] = []
                for m, versions in months.items():
                    if len(versions) < 2:
                        continue
                    present = [v for v in versions if v in shown]
                    # Only audit months the slide actually DISPLAYS (a month entirely absent has
                    # scrolled off the rolling window, or the latest month lives on a fallback slide
                    # owned by the release_columns check). A partial month is the real gap.
                    if not present:
                        continue
                    miss = [v for v in versions if v not in shown]
                    if miss:
                        missing_all.extend(miss)
                        gap["notes"].append(
                            f"slide {n} ({product}): {m} release(s) skipped: {miss} "
                            f"(shown for that month: {sorted(set(present))})")
                gap["slides"][str(n)] = missing_all
                if missing_all:
                    gap["ok"] = False
        results["release_gap"] = gap

        # --- 10. Featured photo consistency (LOOP-HARDENING) -----------------------------
        # Each featured event should show its OWN decluttered per-city capture when one was approved
        # (status "ok"); a configured series hero is the fallback only when the event has no own capture.
        # The embedded hero must match whichever the editor would pick (mirrors pick_event_image).
        photocov = {"slides": {}, "ok": True, "notes": []}
        fallbacks = list((event_images.get("fallbacks") or {}).values())
        images = event_images.get("images") or {}
        for entry in cfg.get("slides", []):
            if entry.get("type") != "in_person_event":
                continue
            ns = entry["n"]
            ns = ns if isinstance(ns, list) else [ns]
            for i, n in enumerate(ns):
                if i >= len(featured):
                    continue
                ev = featured[i]
                own = images.get(ev.get("url", ""))
                if own and own.get("status") == "ok" and own.get("file"):
                    expected_file, kind = own["file"], "own"
                else:
                    fb = _matching_series_fallback(ev, fallbacks)
                    if fb is None:
                        photocov["slides"][str(n)] = []
                        continue
                    expected_file, kind = fb["file"], "series"
                notes = []
                f = unpacked / "ppt" / "slides" / f"slide{n}.xml"
                rid = _hero_pic_embed(f)
                actual = _slide_rel_target(f, rid) if rid else None
                actual_hash = _sha256(actual)
                expected_hash = _sha256((DATA_DIR.parent / expected_file).resolve())
                if not actual_hash:
                    notes.append("cannot resolve embedded featured-event photo")
                elif actual_hash != expected_hash:
                    notes.append(
                        f"embedded photo does not match expected {kind} hero "
                        f"(actual {actual_hash[:10]}, expected {expected_hash[:10] if expected_hash else 'missing'})")
                photocov["slides"][str(n)] = notes
                if notes:
                    photocov["ok"] = False
                    for m in notes:
                        photocov["notes"].append(f"slide {n} (featured photo): {m}")
        results["photo_consistency"] = photocov
    finally:
        shutil.rmtree(unpacked, ignore_errors=True)
        if orig_unpacked is not None:
            shutil.rmtree(orig_unpacked, ignore_errors=True)

    overall = all(r.get("ok", True) for r in results.values())
    results["overall"] = {"ok": overall}

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print(f"\n=== verify_deck: {deck.name} ===")
        for name, r in results.items():
            if name == "overall":
                continue
            status = "PASS" if r.get("ok", True) else "FAIL"
            print(f"[{status}] {name}")
            for note in r.get("notes", []):
                print(f"        - {note}")
        ha = results["hide_audit"]
        print(f"        hidden positions: {ha['hidden_positions']}")
        for n, s in results["release_columns"]["slides"].items():
            print(f"        slide {n}: headers={s['headers']} entries={s['entry_counts']} latest={s['expected_latest']} ok={s['col3_has_latest']}")
        print(f"\n=== OVERALL: {'PASS' if overall else 'FAIL'} ===")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
