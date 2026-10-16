#!/usr/bin/env python3
"""Cut the weekly-build TEMPLATE out of a reference cadence deck.

The reference deck (the full, hand-maintained customer deck) defines how every automated slide looks.
This script keeps only the slides listed in config/deck.yaml, stamps each with its role, and writes a
slim template the weekly build renders into. Run it again whenever the reference deck's design
changes; nothing else needs to know which slide number anything was on.

    python scripts/make_template.py "CSE EMEA Customer Cadence Deck OCTOBER 2026.pptx"

What it does:
  1. finds each role's slide by its text (config/deck.yaml `detect`), not by position;
  2. drops every other slide, plus the speaker notes and comments of the kept ones;
  3. removes layouts and media nothing uses any more, re-encodes oversized pictures (the reference
     deck carries 4K artwork for a 10-inch slide), and drops the source deck's author / slide-title
     metadata;
  4. seeds state/ from what the reference deck currently shows, so curated wording carries over:
     blog history (state/blogs.json) and the highlights and event descriptions a person already
     wrote (state/enrichment.json). Existing state entries are kept unless --reseed is given.

The reference deck is only read. Exit 1 if a required role cannot be found.
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
import tempfile
from datetime import date
from pathlib import Path
from xml.dom import minidom

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import deck, slides as sl, state  # noqa: E402
from lib import pptx_edit as px  # noqa: E402
from lib.common import MONTH_NAMES, REPO_ROOT, load_config, log, today  # noqa: E402

LAYOUT_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"
INVISIBLE = dict.fromkeys([0x200B, 0x200C, 0x200D, 0xFEFF, 0x00AD, 0x2060], None)


# --------------------------------------------------------------------------- role detection -------
def shape_texts(slide: Path) -> list[str]:
    dom = minidom.parse(str(slide))
    out = []
    for sp in dom.getElementsByTagName("p:sp"):
        text = " ".join(px.shape_text(sp).translate(INVISIBLE).split())
        if text:
            out.append(text)
    return out


def assign_roles(ordered: list[Path], rules: list[dict]) -> dict[str, Path]:
    """role -> slide. Roles are searched in config order, each after the previous role's slide, so a
    slide that only MENTIONS a later section (an agenda) cannot claim its role; a role not found that
    way is searched across the whole deck."""
    texts = [shape_texts(s) for s in ordered]
    found: dict[str, Path] = {}
    taken: set[Path] = set()
    cursor = 0
    for rule in rules:
        rx = re.compile(rule["detect"], re.I)
        stop = min(len(ordered), int(rule.get("within_first", len(ordered))))
        for start in (cursor, 0):
            hit = next((i for i in range(start, stop)
                        if ordered[i] not in taken and any(rx.search(t) for t in texts[i])), None)
            if hit is not None:
                found[rule["role"]] = ordered[hit]
                taken.add(ordered[hit])
                cursor = max(cursor, hit + 1)
                break
    return found


# --------------------------------------------------------------------------- slimming -------------
def prune_layouts(unpacked: Path, kept: list[Path]) -> int:
    """Delete slide layouts no kept slide uses (and unhook them from their master). The media only
    those layouts referenced becomes orphaned and is removed by the following clean()."""
    used = set()
    for slide in kept:
        for rel in minidom.parse(str(deck.rels_path(slide))).getElementsByTagName("Relationship"):
            if rel.getAttribute("Type") == LAYOUT_REL:
                used.add(rel.getAttribute("Target").rsplit("/", 1)[-1])
    removed = 0
    masters = unpacked / "ppt" / "slideMasters"
    for master in sorted(masters.glob("slideMaster*.xml")):
        rels_file = masters / "_rels" / f"{master.name}.rels"
        rels = minidom.parse(str(rels_file))
        dom = minidom.parse(str(master))
        dead_rids = set()
        for rel in list(rels.getElementsByTagName("Relationship")):
            name = rel.getAttribute("Target").rsplit("/", 1)[-1]
            if rel.getAttribute("Type") == LAYOUT_REL and name not in used:
                dead_rids.add(rel.getAttribute("Id"))
                rel.parentNode.removeChild(rel)
                layout = unpacked / "ppt" / "slideLayouts" / name
                layout.unlink(missing_ok=True)
                (layout.parent / "_rels" / f"{name}.rels").unlink(missing_ok=True)
                removed += 1
        for lid in list(dom.getElementsByTagName("p:sldLayoutId")):
            if lid.getAttribute("r:id") in dead_rids:
                lid.parentNode.removeChild(lid)
        rels_file.write_text(rels.toxml(), encoding="utf-8")
        master.write_text(dom.toxml(), encoding="utf-8")
    drop_missing_overrides(unpacked)
    return removed


def drop_part(unpacked: Path, part: str, owner_rels: Path) -> None:
    """Remove a part (path relative to the package root) and the relationship that points at it."""
    (unpacked / part).unlink(missing_ok=True)
    if owner_rels.exists():
        dom = minidom.parse(str(owner_rels))
        for rel in list(dom.getElementsByTagName("Relationship")):
            if rel.getAttribute("Target").rsplit("/", 1)[-1] == part.rsplit("/", 1)[-1]:
                rel.parentNode.removeChild(rel)
        owner_rels.write_text(dom.toxml(), encoding="utf-8")


def drop_missing_overrides(unpacked: Path) -> None:
    """Remove [Content_Types] overrides whose part no longer exists."""
    ct = unpacked / "[Content_Types].xml"
    dom = minidom.parse(str(ct))
    for ov in list(dom.getElementsByTagName("Override")):
        if not (unpacked / ov.getAttribute("PartName").lstrip("/")).exists():
            ov.parentNode.removeChild(ov)
    ct.write_text(dom.toxml(), encoding="utf-8")


MEDIA_MIN_BYTES = 400_000   # smaller pictures are left alone
MEDIA_MAX_SIDE = 2560       # px; a 10-inch slide at 256 dpi
JPEG_QUALITY = 90


def optimize_media(unpacked: Path) -> int:
    """Re-encode oversized PNG pictures: scale them to at most MEDIA_MAX_SIDE px and store opaque ones
    as JPEG (photos and gradients compress ~10x better). A picture that uses transparency stays a
    PNG. Every relationship that pointed at a renamed file is updated. Returns the bytes saved."""
    from PIL import Image  # lazy: only needed when cutting a template

    media = unpacked / "ppt" / "media"
    renamed: dict[str, str] = {}
    saved = 0
    for src in sorted(media.glob("*.png")):
        before = src.stat().st_size
        if before < MEDIA_MIN_BYTES:
            continue
        with Image.open(src) as im:
            im.load()
            opaque = im.mode in ("RGB", "L") or (
                im.mode in ("RGBA", "LA") and im.getchannel("A").getextrema()[0] == 255)
            scale = min(1.0, MEDIA_MAX_SIDE / max(im.size))
            if scale < 1.0:
                im = im.resize((round(im.width * scale), round(im.height * scale)), Image.LANCZOS)
            dst = src.with_suffix(".jpeg") if opaque else src
            if opaque and dst.exists():
                continue  # never clobber another media file
            tmp = src.with_name(src.stem + ".opt" + dst.suffix)
            if opaque:
                im.convert("RGB").save(tmp, "JPEG", quality=JPEG_QUALITY, optimize=True)
            else:
                im.save(tmp, "PNG", optimize=True)
        if tmp.stat().st_size >= before:
            tmp.unlink()
            continue
        saved += before - tmp.stat().st_size
        if dst != src:
            src.unlink()
            renamed[src.name] = dst.name
        tmp.replace(dst)
    if renamed:
        for rels in (unpacked / "ppt").rglob("*.rels"):
            text = rels.read_text(encoding="utf-8")
            new_text = text
            for old_name, new_name in renamed.items():
                new_text = new_text.replace(f"media/{old_name}\"", f"media/{new_name}\"")
            if new_text != text:
                rels.write_text(new_text, encoding="utf-8")
        ct = unpacked / "[Content_Types].xml"
        text = ct.read_text(encoding="utf-8")
        if 'Extension="jpeg"' not in text:
            ct.write_text(text.replace("</Types>", '<Default Extension="jpeg" ContentType="image/jpeg"/></Types>'),
                          encoding="utf-8")
    return saved


# --------------------------------------------------------------------------- seeding --------------
def display_to_iso(display: str, as_of: date) -> str | None:
    """'September 28' -> the most recent such date on or before `as_of`."""
    m = re.match(r"\s*([A-Za-z]+)\.?\s+(\d{1,2})", display or "")
    if not m:
        return None
    month = next((i for i, name in enumerate(MONTH_NAMES, 1)
                  if name.lower().startswith(m.group(1).lower()[:3])), None)
    if month is None:
        return None
    try:
        day = date(as_of.year, month, int(m.group(2)))
    except ValueError:
        return None
    return (day if day <= as_of else day.replace(year=as_of.year - 1)).isoformat()


def collect_seed(roles: dict[str, Path], rules: list[dict], slide_cx: int, as_of: date) -> dict:
    """What the reference deck currently shows, in state shape: {"blogs", "highlights", "events",
    "blog_titles"}."""
    seed = {"blogs": {}, "highlights": {}, "events": {}, "blog_titles": {}}
    for rule in rules:
        slide = roles.get(rule["role"])
        if slide is None:
            continue
        dom = minidom.parse(str(slide))
        _, rid_target = px.load_rels(deck.rels_path(slide))
        product = (rule.get("source") or {}).get("product")
        if rule["kind"] == "blogs":
            for grp in sl.blog_groups(dom, slide_cx):
                entry = sl.read_blog_entry(grp["box"], rid_target)
                iso = display_to_iso(entry["date_display"], as_of)
                if not (iso and entry["url"] and entry["title"]):
                    continue
                seed["blogs"].setdefault(product, []).append(
                    {"date": iso, "title": sl.strip_markup(entry["title"]), "url": entry["url"],
                     "source": "reference-deck"})
                if "**" in entry["title"]:
                    seed["blog_titles"][state.norm_url(entry["url"])] = {
                        "title_markup": entry["title"], "source": "reference-deck"}
        elif rule["kind"] == "columns" and (rule.get("style") or {}).get("seed", True):
            for col in sl.columns(dom):
                for entry in sl.read_entries(col["body"], rid_target):
                    key = entry["versions"][0] if entry["versions"] else entry["date"]
                    bullets = [b for b in entry["bullets"] if sl.strip_markup(b).strip()]
                    if key and bullets:
                        seed["highlights"][f"{product}/{key}"] = {"bullets": bullets,
                                                                  "source": "reference-deck"}
        elif rule["kind"] == "webinars":
            for slot in sl.webinar_slots(dom):
                info = sl.read_slot(slot, rid_target)
                if info["url"] and info["description"]:
                    seed["events"][state.event_key(info["url"])] = {"description": info["description"],
                                                                    "source": "reference-deck"}
    return seed


def apply_seed(seed: dict, reseed: bool) -> dict:
    """Merge the seed into state/. Returns counts of what was added."""
    added = {"blogs": 0, "highlights": 0, "events": 0, "blog_titles": 0}
    history = state.load("blogs.json")
    for product, posts in seed["blogs"].items():
        # The deck is the weakest source: it only contributes posts the history does not know yet
        # (by link or by title — a hand-pasted link in the deck can be wrong).
        known = history.get(product, [])
        urls = {state.norm_url(p["url"]) for p in known}
        titles = {state.norm_title(p["title"]) for p in known}
        new = [p for p in posts
               if state.norm_url(p["url"]) not in urls and state.norm_title(p["title"]) not in titles]
        history[product] = state.merge_blog_posts(known, new)
        added["blogs"] += len(new)
    state.save("blogs.json", history)
    enrich = state.load("enrichment.json")
    for section in ("highlights", "events", "blog_titles"):
        bucket = enrich.setdefault(section, {})
        for key, value in seed[section].items():
            if reseed or key not in bucket:
                bucket[key] = value
                added[section] += 1
    state.save("enrichment.json", enrich)
    return added


# --------------------------------------------------------------------------- driver ---------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("reference", help="the full reference deck (.pptx); only read")
    ap.add_argument("--out", default=None, help="template path (default: config/deck.yaml deck.template)")
    ap.add_argument("--as-of", default=None, help="date the reference deck was current (YYYY-MM-DD); "
                    "resolves the year of its 'September 28' style dates. Default: today")
    ap.add_argument("--keep-media", action="store_true", help="do not re-encode oversized pictures")
    ap.add_argument("--no-seed", action="store_true", help="do not touch state/")
    ap.add_argument("--reseed", action="store_true", help="overwrite existing state entries from the deck")
    args = ap.parse_args()

    reference = Path(args.reference).resolve()
    if not reference.exists():
        raise SystemExit(f"reference deck not found: {reference}")
    cfg = load_config("deck.yaml")
    rules = cfg["slides"]
    out = (REPO_ROOT / (args.out or cfg["deck"]["template"])).resolve()
    if out == reference:
        raise SystemExit("refusing to overwrite the reference deck")
    as_of = date.fromisoformat(args.as_of) if args.as_of else today()

    tmp = Path(tempfile.mkdtemp(prefix="template-"))
    try:
        px.unpack(reference, tmp)
        ordered = deck.ordered_slides(tmp)
        roles = assign_roles(ordered, rules)
        missing = [r["role"] for r in rules if r["role"] not in roles]
        required = [r["role"] for r in rules if r["role"] not in roles and not r.get("optional")]
        log(f"reference deck: {reference.name} ({len(ordered)} slides)")
        for rule in rules:
            slide = roles.get(rule["role"])
            where = f"slide {ordered.index(slide) + 1}" if slide else "NOT FOUND"
            log(f"  {rule['role']:<32} {rule['kind']:<9} <- {where}")
        if required:
            log(f"ERROR: required role(s) not found in the reference deck: {required}")
            return 1
        if missing:
            log(f"  (optional roles absent from this deck: {missing})")

        pres = minidom.parse(str(tmp / "ppt" / "presentation.xml"))
        slide_cx = int(pres.getElementsByTagName("p:sldSz")[0].getAttribute("cx"))
        seed = collect_seed(roles, rules, slide_cx, as_of)

        kept = [roles[r["role"]] for r in rules if r["role"] in roles]
        for rule in rules:
            if rule["role"] in roles:
                deck.set_role(roles[rule["role"]], rule["role"])
                deck.strip_private_rels(roles[rule["role"]])
        deck.set_slide_order(tmp, kept)
        deck.clean(tmp)
        layouts = prune_layouts(tmp, kept)
        drop_part(tmp, "ppt/revisionInfo.xml", tmp / "ppt" / "_rels" / "presentation.xml.rels")
        deck.clean(tmp)
        drop_missing_overrides(tmp)
        media_saved = 0 if args.keep_media else optimize_media(tmp)
        deck.stamp_properties(tmp, f"{cfg['deck']['title']} (template)")

        out.parent.mkdir(parents=True, exist_ok=True)
        packed = px.pack(tmp, out, original=reference)
        if packed.returncode != 0:
            log(f"pack failed:\n{packed.stdout[-1500:]}\n{packed.stderr[-800:]}")
            return 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    size_mb = out.stat().st_size / 1e6
    log(f"template -> {out.relative_to(REPO_ROOT)}  ({len(kept)} slides, {layouts} unused layouts "
        f"removed, pictures re-encoded -{media_saved / 1e6:.1f} MB, {size_mb:.1f} MB)")
    if not args.no_seed:
        added = apply_seed(seed, args.reseed)
        log(f"seeded state/: {added['blogs']} blog post(s), {added['highlights']} highlight(s), "
            f"{added['events']} event description(s), {added['blog_titles']} emphasised blog title(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
