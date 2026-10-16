#!/usr/bin/env python3
"""Render the content plan into the template -> the weekly deck.

    python scripts/build_deck.py            # data/deck-plan.json + template -> output/<name>-<date>.pptx

The deck is BUILT, not updated: every run starts from the template (templates/cadence-template.pptx,
cut from the reference deck by make_template.py) and writes each slide's whole content from the plan
(plan_deck.py). Nothing depends on what an earlier deck looked like, so a run is reproducible and a
missed week costs nothing.

Per slide kind:
  * columns   — the three month columns are rewritten: header = month, body = that month's entries.
                Each column uses the richest wording that fits its box; if the template's own line
                spacing cannot hold a month (seven Terraform releases), the slide tightens its spacing
                before it drops any content, and only then shortens the oldest entries away.
  * blogs     — the entry boxes are filled newest first; a box grows by a line for a long title.
  * webinars  — one card per event with its type, link, title, a description cut to whole sentences
                that fit above the icon row, the product icon(s) it is badged with and a language flag.
  * featured  — one slide per featured event (the template slide is duplicated as needed).
  * cover     — month, build date and a "review before sharing" status.

Every slide's layout is then re-established by lib/layout.py, whatever drift the template inherited
from the hand-made reference deck: event cards, flags and product icons on their lines, month headers
level, the blog grid square, highlights of one entry apart, nothing past the slide's edges.

A role the plan has no slide for is left out of the deck (no placeholder, no stale content). Writes
data/build-report.json: what was written where, and every fitting decision that was taken.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from xml.dom import minidom

sys.path.insert(0, str(Path(__file__).resolve().parent))
import edit_event_slides as ev  # noqa: E402  — flag / hero-photo / brand-logo helpers
from lib import compose, deck, layout, slides as sl, textfit  # noqa: E402
from lib import pptx_edit as px  # noqa: E402
from lib.city_photos import build_city_photo_cache, resolve_city_photo  # noqa: E402
from lib.common import DATA_DIR, REPO_ROOT, load_config, log  # noqa: E402
from lib.textfit import EMU_PT  # noqa: E402

OUTPUT_DIR = REPO_ROOT / "output"
SPACING_MODES = ("template", "single", "tight")
FIT_MARGIN = 0.98                # use at most this share of a box's height
BOTTOM_MARGIN = 419100           # EMU (33 pt): how close to the slide bottom a column box may reach
VAULT_ICON_SIZE = (238264, 322485)   # EMU, as placed on the reference deck's cards
TERRAFORM_PURPLE = "7B42BC"
BLOG_TITLE_LINES = 2             # a blog title longer than this is cut with an ellipsis
BLOG_LINE = 139700               # EMU (11 pt) a blog box grows per extra title line
VAULT_ICON = REPO_ROOT / "assets" / "product-icons" / "vault.png"
MONTH_YEAR_RE = re.compile(r"^(January|February|March|April|May|June|July|August|September|October|"
                           r"November|December) \d{4}$")


class BuildError(RuntimeError):
    """The template and the plan do not fit together (a code or template problem, not a data one)."""


class Context:
    def __init__(self, root: Path):
        self.root = root
        pres = minidom.parse(str(root / "ppt" / "presentation.xml"))
        size = pres.getElementsByTagName("p:sldSz")[0]
        self.slide_cx = int(size.getAttribute("cx"))
        self.slide_cy = int(size.getAttribute("cy"))
        self.terraform_icon = None   # prototype <p:grpSp>, found on any webinar slide of the template
        self.city_photos = build_city_photo_cache(None)


def load(slide: Path):
    return minidom.parse(str(slide)), px.load_rels(deck.rels_path(slide))[0]


def save(slide: Path, dom, rels_dom) -> None:
    ev._clean_invisibles(dom)
    px.prune_orphan_hyperlinks(dom, rels_dom)
    ev._prune_orphan_image_rels(dom, rels_dom)
    slide.write_text(dom.toxml(), encoding="utf-8")
    deck.rels_path(slide).write_text(rels_dom.toxml(), encoding="utf-8")


def fresh_ids(node, dom) -> None:
    """Give a cloned shape (and everything inside it) shape ids no other shape on the slide uses."""
    used = [int(c.getAttribute("id")) for c in dom.getElementsByTagName("p:cNvPr")
            if c.getAttribute("id").isdigit()]
    nxt = max(used, default=1) + 1
    for cnv in node.getElementsByTagName("p:cNvPr"):
        cnv.setAttribute("id", str(nxt))
        nxt += 1


def inner_width_pt(sp) -> float:
    left, _top, right, _bottom = compose.insets(sp)
    return (sl.box(sp)[2] - left - right) / EMU_PT


# --------------------------------------------------------------------------- columns --------------
def column_prototypes(bodies: list):
    """(heading paragraph, highlight paragraph) to clone, taken from the newest column that has both."""
    for body in reversed(bodies):
        heading = highlight = None
        for p in px.paragraphs(body):
            if not sl.para_text(p).strip():
                continue
            if sl.is_heading(p):
                heading = heading or p
            elif heading is not None and highlight is None:
                highlight = p
        if heading is not None and highlight is not None:
            return heading, highlight
    raise BuildError("no column with an entry heading and a highlight paragraph to use as prototypes")


def entry_bullets(entry: dict, level: int) -> list[str]:
    return entry["options"][min(level, len(entry["options"]) - 1)]


def render_columns(ctx: Context, slide: Path, plan: dict) -> tuple[list[Path], dict]:
    dom, rels_dom = load(slide)
    cols = sl.columns(dom)
    if len(cols) != len(plan["columns"]):
        raise BuildError(f"{plan['role']}: template has {len(cols)} month columns, plan has "
                         f"{len(plan['columns'])}")
    layout.align_columns(cols)
    bodies = [c["body"] for c in cols]
    heading_proto, bullet_proto = column_prototypes(bodies)
    base_h, base_b = compose.style_of(heading_proto), compose.style_of(bullet_proto, default_size=6.5)
    width = inner_width_pt(bodies[0])
    _l, top, _r, bottom = compose.insets(bodies[0])
    _x, y, _cx, template_cy = sl.box(bodies[0])
    max_cy = ctx.slide_cy - BOTTOM_MARGIN - y
    room = (max_cy - top - bottom) / EMU_PT * FIT_MARGIN

    def styles(mode: str):
        """(heading, first highlight, every further highlight of the same entry) under `mode`."""
        h_style, b_style = textfit.spaced(base_h, mode), textfit.spaced(base_b, mode)
        gap = layout.highlight_gap_pt(h_style.after_pt, b_style.after_pt)
        return h_style, b_style, replace(b_style, before_pt=max(b_style.before_pt, gap))

    def height(entries: list[dict], level: int, mode: str) -> float:
        h_style, b_style, next_style = styles(mode)
        total = 0.0
        for entry in entries:
            total += textfit.para_height([(seg["text"], True) for seg in entry["heading"]], h_style, width)
            for j, bullet in enumerate(entry_bullets(entry, level)):
                total += textfit.para_height(sl.split_markup(bullet), next_style if j else b_style, width)
        return total

    def richest_level(entries: list[dict], mode: str) -> int | None:
        levels = max(len(e["options"]) for e in entries)
        return next((lv for lv in range(levels) if height(entries, lv, mode) <= room), None)

    # Spacing is one decision for the whole slide, wording one per column. Take the loosest spacing
    # under which every column shows its richest wording; failing that, the spacing that keeps the
    # most wording.
    choice = None
    for mode in SPACING_MODES:
        levels = [richest_level(c["entries"], mode) for c in plan["columns"]]
        if None in levels:
            continue
        if choice is None or sum(levels) < sum(choice[1]):
            choice = (mode, levels)
        if not any(levels):
            break
    trimmed = [0] * len(cols)
    shown = [list(c["entries"]) for c in plan["columns"]]
    if choice is None:
        # Even terse wording at the tightest spacing overflows somewhere: keep the newest entries.
        mode = SPACING_MODES[-1]
        levels = []
        for i, entries in enumerate(shown):
            level = richest_level(entries, mode)
            while level is None and len(entries) > 1:
                entries.pop()
                trimmed[i] += 1
                level = richest_level(entries, mode)
            levels.append(level if level is not None else max(len(e["options"]) for e in entries) - 1)
    else:
        mode, levels = choice

    _h, b_style, next_style = styles(mode)

    def highlight(segments: list[dict], follows_highlight: bool):
        p = compose.paragraph(bullet_proto, segments, spacing=mode)
        if follows_highlight and next_style.before_pt != b_style.before_pt:
            compose.set_space_before(p, next_style.before_pt)
        return p

    report_cols, tallest = [], 0.0
    for col, cplan, entries, level, cut in zip(cols, plan["columns"], shown, levels, trimmed):
        paragraphs, written = [], []
        for entry in entries:
            paragraphs.append(compose.paragraph(heading_proto, entry["heading"], rels_dom, spacing=mode))
            bullets = entry_bullets(entry, level)
            for j, bullet in enumerate(bullets):
                paragraphs.append(highlight(compose.markup_segments(bullet), j > 0))
            written.append({"key": entry["key"],
                            "heading": "".join(seg["text"] for seg in entry["heading"]),
                            "links": [seg["url"] for seg in entry["heading"] if seg.get("url")],
                            "bullets": bullets})
        if cut:
            note = f"+ {cut} earlier {'entry' if cut == 1 else 'entries'} this month"
            paragraphs.append(highlight([{"text": note, "bold": False}], True))
        compose.replace_paragraphs(col["body"], paragraphs)
        header_paras = px.paragraphs(col["header"])
        if not header_paras or not compose.set_text(header_paras[0], cplan["header"]):
            raise BuildError(f"{plan['role']}: month header shape has no text run to write into")
        used = height(entries, level, mode)
        tallest = max(tallest, used)
        report_cols.append({"header": cplan["header"], "month": cplan["month"], "level": level,
                            "trimmed": cut, "height_pt": round(used, 1), "entries": written})
    needed_cy = math.ceil(tallest / FIT_MARGIN * EMU_PT) + top + bottom
    new_cy = min(max_cy, max(template_cy, needed_cy))
    for body in bodies:
        sl.set_box(body, cy=new_cy)
    save(slide, dom, rels_dom)
    return [slide], {"spacing": mode, "box_height_pt": round(new_cy / EMU_PT, 1),
                     "room_pt": round(room, 1), "columns": report_cols}


# --------------------------------------------------------------------------- blogs ----------------
def render_blogs(ctx: Context, slide: Path, plan: dict) -> tuple[list[Path], dict]:
    dom, rels_dom = load(slide)
    groups = sl.blog_groups(dom, ctx.slide_cx)
    if not groups:
        raise BuildError(f"{plan['role']}: no blog entry boxes found in the template slide")
    layout.align_blog_grid(groups, ctx.slide_cx)
    one_line_cy = min(sl.box(g["box"])[3] for g in groups)
    written = []
    for i, grp in enumerate(groups):
        if i >= len(plan["entries"]):
            px.remove_node(grp["group"])    # fewer posts than boxes: no empty or stale box
            continue
        entry, box_sp = plan["entries"][i], grp["box"]
        paras = px.paragraphs(box_sp)
        if len(paras) < 2:
            raise BuildError(f"{plan['role']}: blog entry box {i + 1} is not a date line + title line")
        date_run = compose.first_run(paras[0], linked=False)
        link_run = compose.first_run(paras[0], linked=True)
        title_run = compose.first_run(paras[1])
        if date_run is None or link_run is None or title_run is None:
            raise BuildError(f"{plan['role']}: blog entry box {i + 1} lacks a date, link or title run")
        old_date = px.run_text(date_run)
        lead = old_date[:len(old_date) - len(old_date.lstrip())]   # room for the calendar icon
        date_line = compose.empty_clone(paras[0])
        compose.add_run(date_line, date_run, f"{lead}{entry['date_display']} – ")
        compose.add_run(date_line, link_run, "See here", url=entry["url"], rels_dom=rels_dom)

        title = entry["title"]
        style = compose.style_of(paras[1])
        width = inner_width_pt(box_sp)
        lines = textfit.line_count(sl.split_markup(title), style.size_pt, width)
        if lines > BLOG_TITLE_LINES:
            title = textfit.fit_text(sl.strip_markup(title), style, width, BLOG_TITLE_LINES,
                                     whole_sentences=False)
            lines = BLOG_TITLE_LINES
        title_line = compose.paragraph(paras[1], compose.markup_segments(title))
        compose.replace_paragraphs(box_sp, [date_line, title_line])
        # The box is "resize shape to fit text", which PowerPoint only recomputes when the text is
        # edited by hand — so the stored height has to match the line count now.
        new_cy = one_line_cy + (lines - 1) * BLOG_LINE
        sl.set_box(box_sp, cy=new_cy)
        for xf in grp["group"].getElementsByTagName("a:xfrm"):
            if xf.parentNode.parentNode is grp["group"]:
                for child in xf.childNodes:
                    if child.nodeName in ("a:ext", "a:chExt"):
                        child.setAttribute("cy", str(new_cy))
        written.append({"date_display": entry["date_display"], "title": sl.strip_markup(title),
                        "url": entry["url"], "title_lines": lines})
    save(slide, dom, rels_dom)
    return [slide], {"boxes": len(groups), "entries": written}


# --------------------------------------------------------------------------- webinars -------------
def find_terraform_icon(dom):
    """The vector Terraform mark on an event card: a small group whose shapes are all filled with
    Terraform purple. (Only shape fills count — every shape also carries a black text colour.)"""
    for grp in sl.top_level(dom, "p:grpSp"):
        gb = sl.box(grp)
        fills = []
        for sppr in grp.getElementsByTagName("p:spPr"):
            for fill in [c for c in sppr.childNodes if c.nodeName == "a:solidFill"]:
                fills += [c.getAttribute("val").upper() for c in fill.getElementsByTagName("a:srgbClr")]
        if gb and gb[2] < sl.EMU_IN and fills and set(fills) == {TERRAFORM_PURPLE}:
            return grp
    return None


def vault_icon(dom, rels_dom, root: Path):
    """A fresh picture of the Vault mark (assets/product-icons/vault.png)."""
    media = root / "ppt" / "media"
    media.mkdir(parents=True, exist_ok=True)
    name = "image_icon_vault.png"
    if not (media / name).exists():
        shutil.copy(VAULT_ICON, media / name)
    rid = px.add_image_rel(rels_dom, f"../media/{name}")
    pic = minidom.parseString(
        '<p:pic xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<p:nvPicPr><p:cNvPr id="0" name="Vault icon" descr="Vault"/><p:cNvPicPr>'
        '<a:picLocks noChangeAspect="1"/></p:cNvPicPr><p:nvPr/></p:nvPicPr>'
        f'<p:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>'
        '<p:spPr><a:xfrm><a:off x="0" y="0"/>'
        f'<a:ext cx="{VAULT_ICON_SIZE[0]}" cy="{VAULT_ICON_SIZE[1]}"/></a:xfrm>'
        '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>').documentElement
    for attr in ("xmlns:p", "xmlns:a", "xmlns:r"):
        pic.removeAttribute(attr)   # the slide root already declares these prefixes
    return dom.importNode(pic, True)


def place_card_icons(ctx: Context, dom, rels_dom, slot_sp, products: list[str]) -> list[str]:
    """One icon per product on the card, placed by layout.place_icons (bottom-left, one top line)."""
    nodes, placed = [], []
    for product in products:
        if product == "terraform" and ctx.terraform_icon is not None:
            node = dom.importNode(ctx.terraform_icon, True)
        elif product == "vault" and VAULT_ICON.exists():
            node = vault_icon(dom, rels_dom, ctx.root)
        else:
            continue
        fresh_ids(node, dom)
        sl.sp_tree(dom).appendChild(node)
        nodes.append(node)
        placed.append(product)
    layout.place_icons(nodes, slot_sp)
    return placed


def render_webinars(ctx: Context, slide: Path, plan: dict) -> tuple[list[Path], dict]:
    dom, rels_dom = load(slide)
    slots = sl.webinar_slots(dom)
    if not slots:
        raise BuildError(f"{plan['role']}: no event cards found in the template slide")
    events = plan["slots"][:len(slots)]
    on_card = [sl.slot_decorations(dom, slot) for slot in slots]   # before any card moves
    layout.align_cards(slots)
    written = []
    for i, (slot, decorations) in enumerate(zip(slots, on_card)):
        if i >= len(events):
            for node in decorations:     # a card without an event goes, with its flag and icons
                px.remove_node(node)
            px.remove_node(slot)
            continue
        event = events[i]
        paras = px.paragraphs(slot)
        head_run = compose.first_run(paras[0], linked=False)
        link_run = compose.first_run(paras[0], linked=True)
        desc_proto = next((p for p in paras[2:] if sl.para_text(p).strip()), None)
        if head_run is None or link_run is None or desc_proto is None:
            raise BuildError(f"{plan['role']}: event card {i + 1} lacks a date run, a link run or a "
                             "description paragraph to use as prototypes")
        head = compose.empty_clone(paras[0])
        compose.add_run(head, head_run, f"{event['date_label']} | {event['kind']} | ")
        compose.add_run(head, link_run, "See here", url=event["url"], rels_dom=rels_dom)
        title = compose.paragraph(paras[1], [{"text": event["title"]}])

        # The description takes the lines left between the title and the icon row, in whole sentences.
        width = inner_width_pt(slot)
        _l, top, _r, _b = compose.insets(slot)
        room = (sl.box(slot)[3] - top - layout.CARD_ICON_ROW) / EMU_PT
        head_style, title_style = compose.style_of(paras[0]), compose.style_of(paras[1])
        desc_style = compose.style_of(desc_proto, default_size=6.5)
        fixed = (textfit.para_height([(sl.para_text(head), False)], head_style, width)
                 + textfit.para_height([(event["title"], True)], title_style, width))
        max_lines = int((room - fixed - desc_style.before_pt - desc_style.after_pt)
                        // desc_style.line_height())
        description = ""
        for option in event["description_options"]:
            description = textfit.fit_text(option, desc_style, width, max(max_lines, 1))
            if description:
                break
        desc = compose.paragraph(desc_proto, [{"text": description, "bold": False}])
        compose.replace_paragraphs(slot, [head, title, desc])

        flag = None
        for node in decorations:
            if sl.is_flag(node):         # the card's own flag follows the event's language
                flag = ev.set_slot_flag(node, rels_dom, ctx.root, deck.slide_number(slide), i,
                                        event.get("language", "en")) or flag
                layout.place_flag(node, slot)
            else:
                px.remove_node(node)     # template icons; the event's own are placed below
        icons = place_card_icons(ctx, dom, rels_dom, slot, event["products"])
        written.append({"date_label": event["date_label"], "kind": event["kind"], "title": event["title"],
                        "url": event["url"], "description": description, "description_lines": max_lines,
                        "icons": icons, "language": event.get("language", "en"),
                        "flag_changed": bool(flag)})
    save(slide, dom, rels_dom)
    return [slide], {"cards": len(slots), "events": written,
                     "not_shown": [f"{e['date']} {e['title']}" for e in plan["slots"][len(slots):]]
                     + plan.get("later", [])}


# --------------------------------------------------------------------------- featured -------------
def shrink_to_fit(sp, paragraphs: list[list[tuple[str, bool]]], *, one_line: bool = False) -> int:
    """Make a text box's content fit by storing a font scale (PowerPoint's "shrink text on overflow"),
    which PowerPoint honours on open but only ever computes itself while someone is typing. Returns
    the scale in percent (100 = untouched). Text that fits is left exactly as the template has it."""
    style = compose.style_of(px.paragraphs(sp)[0], default_size=12.0, shape=sp)
    left, top, right, bottom = compose.insets(sp)
    _x, _y, cx, cy = sl.box(sp)
    width = (cx - left - right) / EMU_PT
    if one_line:
        need = max(sum(textfit.text_width(t, style.size_pt, b) for t, b in para) for para in paragraphs)
        ratio = (width - style.indent_pt) / (need * textfit.SAFETY) if need else 1.0
    else:
        room = (cy - top - bottom) / EMU_PT
        ratio = 1.0
        for scale in (1.0, 0.95, 0.9, 0.85, 0.8, 0.75, 0.7, 0.65, 0.6):
            scaled = textfit.ParaStyle(size_pt=style.size_pt * scale, bold=style.bold,
                                       line_pct=style.line_pct, before_pt=style.before_pt * scale,
                                       after_pt=style.after_pt * scale, indent_pt=style.indent_pt)
            ratio = scale
            if sum(textfit.para_height(para, scaled, width) for para in paragraphs) <= room:
                break
    if ratio >= 1.0:
        return 100
    percent = max(60, int(ratio * 100) // 5 * 5)
    body = sp.getElementsByTagName("a:bodyPr")[0]
    for tag in ("a:spAutoFit", "a:normAutofit", "a:noAutofit"):
        for el in list(body.getElementsByTagName(tag)):
            px.remove_node(el)
    fit = sp.ownerDocument.createElement("a:normAutofit")
    fit.setAttribute("fontScale", str(percent * 1000))
    body.insertBefore(fit, body.firstChild)
    return percent


def write_featured(ctx: Context, slide: Path, event: dict) -> dict:
    dom, rels_dom = load(slide)
    parts = sl.featured_parts(dom)
    if any(parts.get(k) is None for k in ("title", "subtitle", "description")):
        raise BuildError("featured-event template slide lacks a title, an 'Event | …' line or a description")
    compose.set_text(px.paragraphs(parts["title"])[0], event["title"])
    title_scale = shrink_to_fit(parts["title"], [[(event["title"], True)]], one_line=True)

    line = px.paragraphs(parts["subtitle"])[0]
    plain = compose.first_run(line, linked=False)
    linked = compose.first_run(line, linked=True)
    if plain is None or linked is None:
        raise BuildError("featured-event 'Event | …' line lacks a plain run or a link run")
    new_line = compose.empty_clone(line)
    compose.add_run(new_line, plain, f"Event | {event['date_display']} | ")
    for i, link in enumerate(event["links"]):
        if i:
            compose.add_run(new_line, plain, " | ")
        compose.add_run(new_line, linked, link["label"], url=link["url"], rels_dom=rels_dom)
    line.parentNode.replaceChild(new_line, line)

    desc_proto = next(p for p in px.paragraphs(parts["description"]) if sl.para_text(p).strip())
    points = [compose.paragraph(desc_proto, compose.markup_segments(pt)) for pt in event["description_points"]]
    compose.replace_paragraphs(parts["description"], points)
    desc_scale = shrink_to_fit(parts["description"],
                               [sl.split_markup(pt) for pt in event["description_points"]])

    visuals = "template"
    if event.get("visuals") != "template":
        # The template's pictures show the event the slide was made for. Any other event gets its city
        # photo; with none configured the event pictures are removed rather than left to mislead.
        photo = resolve_city_photo(event.get("location", ""), ctx.city_photos)
        hero = ev.find_hero_pic(dom)
        if photo and hero is not None:
            ev.swap_event_image(dom, rels_dom, ctx.root, deck.slide_number(slide), Path(photo))
            ev.remove_nonhero_pics(dom, rels_dom, hero)
            ev.place_brand_logo(dom, rels_dom, ctx.root, hero, deck.slide_number(slide))
            visuals = "city-photo"
        else:
            if hero is not None:
                ev.remove_nonhero_pics(dom, rels_dom, hero)
                px.remove_node(hero)
            visuals = "none"
    icons = ev.match_product_icons(dom, rels_dom, ctx.root, event)
    save(slide, dom, rels_dom)
    return {"title": event["title"], "date_display": event["date_display"], "links": event["links"],
            "description_points": [sl.strip_markup(p) for p in event["description_points"]],
            "title_scale": title_scale, "description_scale": desc_scale, "visuals": visuals,
            "product_icons": icons}


def render_featured(ctx: Context, slide: Path, plan: dict) -> tuple[list[Path], dict]:
    used = [slide]
    for _ in plan["events"][1:]:
        copy = deck.duplicate_after(ctx.root, slide, used[-1])   # copies of the pristine template slide
        if copy is None:
            raise BuildError("could not duplicate the featured-event template slide")
        used.append(copy)
    return used, {"events": [write_featured(ctx, s, e) for s, e in zip(used, plan["events"])]}


# --------------------------------------------------------------------------- cover / static -------
def render_cover(ctx: Context, slide: Path, plan: dict) -> tuple[list[Path], dict]:
    dom, rels_dom = load(slide)
    wrote = {"month": False, "status": False, "edition": False}
    for sp in sl.top_level(dom, "p:sp"):
        paras = px.paragraphs(sp)
        text = " ".join(px.shape_text(sp).split())
        if MONTH_YEAR_RE.match(text):
            wrote["month"] = compose.set_text(paras[0], plan["month_label"])
        elif "CURRENT STATUS" in text.upper():
            # A person has not looked at this deck yet, so it must not claim it is ready to publish.
            for p, new in zip([p for p in paras if sl.para_text(p).strip()], plan["status_lines"]):
                compose.set_text(p, new)
            wrote["status"] = True
        elif re.search(r"FY\d\d\s*\|", text):
            wrote["edition"] = compose.set_text(paras[0], f"{plan['fiscal_year']} | {plan['week']}")
    if not wrote["month"]:
        raise BuildError("cover template slide has no '<Month> <Year>' text to write the month into")
    save(slide, dom, rels_dom)
    return [slide], {"month_label": plan["month_label"], "wrote": wrote}


def render_static(ctx: Context, slide: Path, plan: dict) -> tuple[list[Path], dict]:
    return [slide], {}


RENDERERS = {"cover": render_cover, "static": render_static, "columns": render_columns,
             "blogs": render_blogs, "webinars": render_webinars, "featured": render_featured}


# --------------------------------------------------------------------------- driver ---------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--plan", default=str(DATA_DIR / "deck-plan.json"))
    ap.add_argument("--out", default=None, help="output .pptx (default: output/<filename>-<build date>.pptx)")
    ap.add_argument("--report", default=str(DATA_DIR / "build-report.json"))
    args = ap.parse_args()

    cfg = load_config("deck.yaml")
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    if plan["build"]["status"] == "failed":
        log(f"ERROR: the plan is marked failed: {plan['build']['problems']}")
        return 1
    template = REPO_ROOT / cfg["deck"]["template"]
    if not template.exists():
        log(f"ERROR: template not found: {template} — cut one with scripts/make_template.py")
        return 1
    stem = cfg["deck"].get("filename") or re.sub(r"[^a-z0-9]+", "-", cfg["deck"]["title"].lower()).strip("-")
    out = Path(args.out).resolve() if args.out else OUTPUT_DIR / f"{stem}-{plan['build']['date']}.pptx"
    if out.resolve() == template.resolve():
        raise SystemExit("refusing to overwrite the template")

    tmp = Path(tempfile.mkdtemp(prefix="build-"))
    report = {"deck": str(out), "template": str(template.relative_to(REPO_ROOT)),
              "built_at": datetime.now(timezone.utc).isoformat(), "build": plan["build"],
              "slides": [], "omitted": plan["omitted"]}
    try:
        px.unpack(template, tmp)
        roles = deck.slides_by_role(tmp)
        ctx = Context(tmp)
        for slides in roles.values():   # icon prototypes come from the pristine template
            if ctx.terraform_icon is None:
                ctx.terraform_icon = find_terraform_icon(minidom.parse(str(slides[0])))
        order: list[Path] = []
        for slide_plan in plan["slides"]:
            role = slide_plan["role"]
            if role not in roles:
                raise BuildError(f"the template has no slide for role '{role}' — re-cut it with "
                                 "scripts/make_template.py from a reference deck that has one")
            used, detail = RENDERERS[slide_plan["kind"]](ctx, roles[role][0], slide_plan)
            order += used
            report["slides"].append({"role": role, "kind": slide_plan["kind"],
                                     "positions": list(range(len(order) - len(used) + 1, len(order) + 1)),
                                     **detail})
        for slide in order:
            dom, rels_dom = load(slide)
            cropped = layout.keep_inside(dom, ctx.slide_cx, ctx.slide_cy)
            if cropped:
                save(slide, dom, rels_dom)
                report.setdefault("layout", []).extend(f"{deck.get_role(slide)}: {c}" for c in cropped)
        deck.set_slide_order(tmp, order)
        deck.clean(tmp)
        deck.stamp_properties(tmp, f"{cfg['deck']['title']} – {plan['build']['date']}")
        out.parent.mkdir(parents=True, exist_ok=True)
        packed = px.pack(tmp, out, original=template)
        if packed.returncode != 0:
            log(f"ERROR: packing/validation failed:\n{packed.stdout[-2000:]}\n{packed.stderr[-800:]}")
            return 2
    except BuildError as exc:
        log(f"ERROR: {exc}")
        return 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    for s in report["slides"]:
        extra = ""
        if s["kind"] == "columns":
            extra = (f"spacing={s['spacing']} box={s['box_height_pt']}pt "
                     + " | ".join(f"{c['header']}: {len(c['entries'])} (wording {c['level']}"
                                  + (f", -{c['trimmed']} trimmed" if c["trimmed"] else "") + ")"
                                  for c in s["columns"]))
        elif s["kind"] == "blogs":
            extra = f"{len(s['entries'])}/{s['boxes']} boxes"
        elif s["kind"] == "webinars":
            extra = f"{len(s['events'])}/{s['cards']} cards" + (
                f", {len(s['not_shown'])} more not shown" if s["not_shown"] else "")
        elif s["kind"] == "featured":
            extra = "; ".join(f"{e['title']} [{e['visuals']}]" for e in s["events"])
        log(f"  slide {s['positions'][0]:>2} {s['role']:<32} {extra}")
    log(f"deck -> {out.relative_to(REPO_ROOT) if out.is_relative_to(REPO_ROOT) else out}  "
        f"({out.stat().st_size / 1e6:.1f} MB, {sum(len(s['positions']) for s in report['slides'])} slides)")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
