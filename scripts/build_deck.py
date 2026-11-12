#!/usr/bin/env python3
"""Render the content plan into the template -> the weekly deck.

    python scripts/build_deck.py            # data/deck-plan.json + template -> output/<name>-<date>.pptx

The deck is BUILT, not updated: every run starts from the template (templates/cadence-template.pptx,
cut from the reference deck by make_template.py) and writes each slide's whole content from the plan
(plan_deck.py). Nothing depends on what an earlier deck looked like, so a run is reproducible and a
missed week costs nothing.

Per slide kind:
  * columns   — the three month columns are rewritten: header = month, body = that month's entries,
                spaced and sized as on the slide `deck.entries_from` names, in boxes of one height.
                Each column uses the richest wording that fits its box; if that spacing cannot hold a
                month, the slide tightens it before it drops any content, and only then shortens the
                oldest entries away.
  * blogs     — the entry boxes are filled newest first; a box grows by a line for a long title;
                the topic buttons keep their labels on one line.
  * webinars  — one card per event with its type, link, title, a description cut to whole sentences
                that fit above the icon row, the product icon(s) it is badged with and a language flag;
                events beyond the template's row of cards get a second row of copies under it.
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
import re
import shutil
import sys
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from xml.dom import minidom

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import compose, deck, layout, slides as sl, textfit  # noqa: E402
from lib import event_media as ev  # noqa: E402  — flag / hero-photo / brand-logo helpers
from lib import package  # noqa: E402
from lib import pptx_edit as px  # noqa: E402
from lib.city_photos import build_city_photo_cache, resolve_city_photo  # noqa: E402
from lib.common import DATA_DIR, REPO_ROOT, load_config, log  # noqa: E402
from lib.textfit import EMU_PT  # noqa: E402

OUTPUT_DIR = REPO_ROOT / "output"
SPACING_MODES = ("template", "single", "tight")
FIT_MARGIN = 0.98                # use at most this share of a box's height
BOTTOM_MARGIN = 419100           # EMU (33 pt): how close to the slide bottom the column boxes reach
VAULT_ICON_SIZE = (238264, 322485)   # EMU, as placed on the reference deck's cards
TERRAFORM_PURPLE = "7B42BC"
BLOG_TITLE_LINES = 2             # a blog title longer than this is cut with an ellipsis
BLOG_LINE = 139700               # EMU (11 pt) a blog box is taller per title line
BUTTON_INSET = 45720             # EMU (3.6 pt): a topic button's side insets, as its top and bottom ones
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
        self.bullet_like: dict = {}  # role -> highlight paragraph whose bullet that role's columns take
        self.entry_like = None       # (heading, highlight) every slide's entries are spaced and sized like
        self.placed_like: dict = {}  # role -> the places its cards / blog entries take from a sibling slide


def load(slide: Path):
    return minidom.parse(str(slide)), px.load_rels(deck.rels_path(slide))[0]


def save(slide: Path, dom, rels_dom) -> None:
    px.strip_invisibles(dom)
    px.prune_orphan_rels(dom, rels_dom)
    slide.write_text(dom.toxml(), encoding="utf-8")
    deck.rels_path(slide).write_text(rels_dom.toxml(), encoding="utf-8")


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


def kept_whole(segments: list[dict], style: textfit.ParaStyle, width: float, heading: bool = False) -> list[dict]:
    """`segments` with a line break before each word PowerPoint would otherwise cut at a hyphen: a
    heading's date ("(2026-09-" / "16)") or a flag ("-" / "minimal-refresh"). A non-breaking hyphen is
    not in the deck's font, so the word is sent whole to the line it opens anyway (textfit.cut_points)."""
    points = textfit.cut_points([(seg["text"], seg.get("bold", heading)) for seg in segments],
                                style.size_pt, width - style.indent_pt, heading)
    out, at = [], 0
    for seg in segments:
        text, start = seg["text"], 0
        unlinked = {k: v for k, v in seg.items() if k != "url"}
        for point in (p - at for p in points if at <= p < at + len(text)):
            head = text[start:point].rstrip(" ")
            # the space the line ends with is left out of a link, which would underline it
            out += [{**seg, "text": head}, {**unlinked, "text": text[start + len(head):point]}, {"break": True}]
            start = point
        out.append({**seg, "text": text[start:]})
        at += len(text)
    return [seg for seg in out if seg.get("break") or seg["text"]]


def render_columns(ctx: Context, slide: Path, plan: dict) -> tuple[list[Path], dict]:
    dom, rels_dom = load(slide)
    cols = sl.columns(dom)
    if len(cols) != len(plan["columns"]):
        raise BuildError(f"{plan['role']}: template has {len(cols)} month columns, plan has "
                         f"{len(plan['columns'])}")
    layout.align_columns(cols)
    bodies = [c["body"] for c in cols]
    heading_proto, bullet_proto = column_prototypes(bodies)
    if ctx.entry_like:   # every slide's entries spaced and sized alike (deck.entries_from)
        heading_proto, bullet_proto = heading_proto.cloneNode(True), bullet_proto.cloneNode(True)
        compose.take_entry_style(heading_proto, ctx.entry_like[0])
        compose.take_entry_style(bullet_proto, ctx.entry_like[1])
    if plan["role"] in ctx.bullet_like:   # one product, one bullet: the HCP slide takes the release slide's
        bullet_proto = bullet_proto.cloneNode(True)
        compose.take_bullet(bullet_proto, ctx.bullet_like[plan["role"]])
    base_h, base_b = compose.style_of(heading_proto), compose.style_of(bullet_proto, default_size=6.5)
    width = inner_width_pt(bodies[0])
    _l, top, _r, bottom = compose.insets(bodies[0])
    y = sl.box(bodies[0])[1]
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
        p = compose.paragraph(bullet_proto, kept_whole(segments, base_b, width), spacing=mode)
        if follows_highlight and next_style.before_pt != b_style.before_pt:
            compose.set_space_before(p, next_style.before_pt)
        return p

    report_cols = []
    for col, cplan, entries, level, cut in zip(cols, plan["columns"], shown, levels, trimmed):
        paragraphs, written = [], []
        for entry in entries:
            paragraphs.append(compose.paragraph(heading_proto, kept_whole(entry["heading"], base_h, width, True),
                                                rels_dom, spacing=mode))
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
        compose.match_end_mark(header_paras[0])
        used = height(entries, level, mode)
        report_cols.append({"header": cplan["header"], "month": cplan["month"], "level": level,
                            "trimmed": cut, "height_pt": round(used, 1), "entries": written})
    # Every release slide's boxes reach down to the same line, however much this one holds: boxes that
    # grew only as far as their text needed came out a different height on each slide.
    for body in bodies:
        sl.set_box(body, cy=max_cy)
    save(slide, dom, rels_dom)
    return [slide], {"spacing": mode, "box_height_pt": round(max_cy / EMU_PT, 1),
                     "room_pt": round(room, 1), "columns": report_cols}


# --------------------------------------------------------------------------- blogs ----------------
def blog_title(title: str, style: textfit.ParaStyle, width: float, short: str = "") -> tuple[str, int]:
    """(the title as it goes in its box, the lines it takes): at most BLOG_TITLE_LINES. Bold is wider, so
    a title loses its emphasis before any of its words. One too long even plain gives way to `short`, the
    shorter title the wording step made of its own words; only without one is it cut with an ellipsis."""
    for wording in filter(None, (title, short)):
        for text in (wording, sl.strip_markup(wording)):
            lines = textfit.line_count(sl.split_markup(text), style.size_pt, width)
            if lines <= BLOG_TITLE_LINES:
                return text, lines
    return (textfit.fit_text(sl.strip_markup(title), style, width, BLOG_TITLE_LINES, whole_sentences=False),
            BLOG_TITLE_LINES)


def render_blogs(ctx: Context, slide: Path, plan: dict) -> tuple[list[Path], dict]:
    dom, rels_dom = load(slide)
    groups = sl.blog_groups(dom, ctx.slide_cx)
    if not groups:
        raise BuildError(f"{plan['role']}: no blog entry boxes found in the template slide")
    layout.align_blog_grid(groups, ctx.slide_cx)
    layout.place_like([g["group"] for g in groups], ctx.placed_like.get(plan["role"], []))
    # Every box is as tall as the longest title allowed: boxes that followed their own title's
    # lines left uneven gaps between the rows. (They are "resize shape to fit text", which PowerPoint
    # only recomputes when the text is edited by hand, so the height is stored here.)
    box_cy = min(sl.box(g["box"])[3] for g in groups) + (BLOG_TITLE_LINES - 1) * BLOG_LINE
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

        title, lines = blog_title(entry["title"], compose.style_of(paras[1]), inner_width_pt(box_sp),
                                  entry.get("title_short", ""))
        title_line = compose.paragraph(paras[1], compose.markup_segments(title))
        compose.replace_paragraphs(box_sp, [date_line, title_line])
        sl.set_box(box_sp, cy=box_cy)
        for xf in grp["group"].getElementsByTagName("a:xfrm"):
            if xf.parentNode.parentNode is grp["group"]:
                for child in xf.childNodes:
                    if child.nodeName in ("a:ext", "a:chExt"):
                        child.setAttribute("cy", str(box_cy))
        written.append({"date_display": entry["date_display"], "title": sl.strip_markup(title),
                        "url": entry["url"], "title_lines": lines})
    button_scale = fit_buttons(dom)
    for sp in sl.link_buttons(dom):   # the label clicks through too, not only the pill around it
        compose.link_label(sp)
    save(slide, dom, rels_dom)
    return [slide], {"boxes": len(groups), "entries": written, "button_scale": button_scale}


def fit_buttons(dom) -> int:
    """Every topic button's label on one line ("Explore Topics!"): the reference deck's 7 pt labels need
    two lines in their pills and spill out of them. The side insets shrink to the top and bottom ones,
    and where a label still does not fit, every label takes the template's size scaled down by one
    factor, so they stay one size. The size is written into the labels: a stored "shrink on overflow"
    scale is worked out again box by box by other renderers (the PDF preview), which mixed sizes.
    Returns the factor in percent."""
    buttons = sl.link_buttons(dom)
    for sp in buttons:
        body = sp.getElementsByTagName("a:bodyPr")[0]
        body.setAttribute("lIns", str(BUTTON_INSET))
        body.setAttribute("rIns", str(BUTTON_INSET))
    ratio = min([room / need for need, room in map(compose.one_line_pt, buttons) if need] + [1.0])
    percent = 100 if ratio >= 1 else max(60, int(ratio * 100) // 5 * 5)
    if percent < 100:
        for sp in buttons:
            for props in sp.getElementsByTagName("a:rPr") + sp.getElementsByTagName("a:endParaRPr"):
                if props.getAttribute("sz"):
                    props.setAttribute("sz", str(int(props.getAttribute("sz")) * percent // 100))
    return percent


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


def place_card_icons(ctx: Context, dom, rels_dom, slot_sp, products: list[str]) -> list[str]:
    """One icon per product on the card, placed by layout.place_icons (bottom-left, one top line)."""
    nodes, placed = [], []
    for product in products:
        if product == "terraform" and ctx.terraform_icon is not None:
            node = dom.importNode(ctx.terraform_icon, True)
        elif product == "vault" and VAULT_ICON.exists():
            node = px.new_picture(dom, rels_dom, ctx.root, VAULT_ICON, "image_icon_vault.png", "Vault icon",
                                  (0, 0, *VAULT_ICON_SIZE), descr="Vault")
        else:
            continue
        px.fresh_ids(node, dom)
        sl.sp_tree(dom).appendChild(node)
        nodes.append(node)
        placed.append(product)
    layout.place_icons(nodes, slot_sp)
    return placed


def add_second_row(ctx: Context, dom, slots: list, on_card: list, events: int) -> None:
    """More events than the template's row of cards: copies of its cards (with their flags) as a
    second row, placed by layout.place_second_row. `slots` and `on_card` are extended in place."""
    row = layout.card_rows(slots)[0]
    extra = min(events, 2 * len(row)) - len(slots)
    if len(row) != len(slots) or extra <= 0:
        return
    cards = []
    for src in row[:extra]:
        copies = [src] + [n for n in on_card[slots.index(src)] if sl.is_flag(n)]
        copies = [n.cloneNode(True) for n in copies]
        for node in copies:
            px.fresh_ids(node, dom)
            sl.sp_tree(dom).appendChild(node)
        cards.append(copies[0])
        on_card.append(copies[1:])
    layout.place_second_row(row, cards, ctx.slide_cy)
    slots.extend(cards)


def card_description(options: list[str], style: textfit.ParaStyle, width: float, lines: int) -> str:
    """What a card says under its title: the first of `options` that fits in whole sentences. A card of a
    two-row slide is shorter, and a sentence written for a tall card ended mid-phrase there while the
    event's own abstract opened with one that fits. A description is never cut at a word: the plan's
    last options are generic lines, the very last short enough for one line of any card."""
    whole = (textfit.fit_sentences(option, style, width, lines) for option in options)
    return next(filter(None, whole), "") or options[-1]


def render_webinars(ctx: Context, slide: Path, plan: dict) -> tuple[list[Path], dict]:
    dom, rels_dom = load(slide)
    slots = sl.webinar_slots(dom)
    if not slots:
        raise BuildError(f"{plan['role']}: no event cards found in the template slide")
    on_card = [sl.slot_decorations(dom, slot) for slot in slots]   # before any card moves
    layout.place_like(slots, ctx.placed_like.get(plan["role"], []))
    layout.align_cards(slots)
    add_second_row(ctx, dom, slots, on_card, len(plan["slots"]))
    events = plan["slots"][:len(slots)]
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
        gap = layout.description_gap_pt(head_style.after_pt, title_style.before_pt, title_style.after_pt)
        # The description is the card's last paragraph: no space after it, which nothing would use.
        desc_style = replace(compose.style_of(desc_proto, default_size=6.5), before_pt=gap, after_pt=0.0)
        fixed = (textfit.para_height([(sl.para_text(head), False)], head_style, width)
                 + textfit.para_height([(event["title"], True)], title_style, width))
        max_lines = int((room - fixed - desc_style.before_pt - desc_style.after_pt)
                        // desc_style.line_height())
        description = card_description(event["description_options"], desc_style, width, max(max_lines, 1))
        desc = compose.paragraph(desc_proto, [{"text": description, "bold": False}])
        compose.set_space_before(desc, gap)
        compose.set_space_after(desc, 0.0)
        compose.replace_paragraphs(slot, [head, title, desc])

        flag_changed = False
        for node in decorations:
            if sl.is_flag(node):         # the card's own flag follows the event's language
                flag_changed = ev.set_slot_flag(node, rels_dom, ctx.root, deck.slide_number(slide), i,
                                                event.get("language", "en"))
                layout.place_flag(node, slot)
            else:
                px.remove_node(node)     # template icons; the event's own are placed below
        icons = place_card_icons(ctx, dom, rels_dom, slot, event["products"])
        written.append({"date_label": event["date_label"], "kind": event["kind"], "title": event["title"],
                        "url": event["url"], "description": description, "description_lines": max_lines,
                        "icons": icons, "language": event.get("language", "en"),
                        "flag_changed": flag_changed})
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
    set_font_scale(sp, percent)
    return percent


def set_font_scale(sp, percent: int) -> None:
    """Store a font scale on a text box (PowerPoint's "shrink text on overflow")."""
    body = sp.getElementsByTagName("a:bodyPr")[0]
    for tag in ("a:spAutoFit", "a:normAutofit", "a:noAutofit"):
        for el in list(body.getElementsByTagName(tag)):
            px.remove_node(el)
    fit = sp.ownerDocument.createElement("a:normAutofit")
    fit.setAttribute("fontScale", str(percent * 1000))
    body.insertBefore(fit, body.firstChild)


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
    # The points sit level with the middle of the event picture beside them, not at its top.
    parts["description"].getElementsByTagName("a:bodyPr")[0].setAttribute("anchor", "ctr")
    desc_scale = shrink_to_fit(parts["description"],
                               [sl.split_markup(pt) for pt in event["description_points"]])

    visuals = "template"
    if event.get("visuals") != "template":
        # The template's pictures show the event the slide was made for. Any other event gets its city
        # photo; with none configured the event pictures are removed rather than left to mislead.
        photo = resolve_city_photo(event.get("location", ""), ctx.city_photos)
        hero = ev.find_hero_pic(dom)
        if photo and hero is not None:
            ev.swap_event_image(hero, rels_dom, ctx.root, deck.slide_number(slide), Path(photo))
            ev.remove_nonhero_pics(dom, hero)
            ev.place_brand_logo(dom, rels_dom, ctx.root, hero, deck.slide_number(slide))
            visuals = "city-photo"
        else:
            if hero is not None:
                ev.remove_nonhero_pics(dom, hero)
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
        used.append(deck.duplicate_after(ctx.root, slide, used[-1]))   # copies of the pristine template slide
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
        package.unpack(template, tmp)
        roles = deck.slides_by_role(tmp)
        ctx = Context(tmp)
        for slides in roles.values():   # icon prototypes come from the pristine template
            if ctx.terraform_icon is None:
                ctx.terraform_icon = find_terraform_icon(minidom.parse(str(slides[0])))
        def pristine(source: str, key: str):     # and so does whatever one slide borrows from another
            if source not in roles:
                raise BuildError(f"{key} names '{source}', which the template lacks")
            return minidom.parse(str(roles[source][0]))

        def prototypes(source: str, key: str):
            return column_prototypes([c["body"] for c in sl.columns(pristine(source, key))])
        if cfg["deck"].get("entries_from"):
            ctx.entry_like = prototypes(cfg["deck"]["entries_from"], "deck.entries_from")
        for rule in cfg["slides"]:
            source = (rule.get("style") or {}).get("bullets_from")
            if source:
                ctx.bullet_like[rule["role"]] = prototypes(source, f"{rule['role']}: bullets_from")[1]
            if rule.get("placed_like"):
                sibling = pristine(rule["placed_like"], f"{rule['role']}: placed_like")
                ctx.placed_like[rule["role"]] = layout.places(sibling, rule["kind"], ctx.slide_cx)
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
        loaded = {slide: load(slide) for slide in order}        # the rules that span the whole deck
        subtitles = [sl.subtitle(dom) for dom, _rels in loaded.values()]
        layout.align_subtitles([s for s in subtitles if s is not None])
        for slide, (dom, rels_dom) in loaded.items():
            cropped = layout.keep_inside(dom, ctx.slide_cx, ctx.slide_cy)
            compose.unlink_spaces(dom)
            save(slide, dom, rels_dom)
            report.setdefault("layout", []).extend(f"{deck.get_role(slide)}: {c}" for c in cropped)
        deck.set_slide_order(tmp, order)
        deck.clean(tmp)
        # Dated by the plan, not the clock: the same plan gives the same file, byte for byte.
        deck.stamp_properties(tmp, f"{cfg['deck']['title']} – {plan['build']['date']}",
                              when=datetime.fromisoformat(plan["build"]["date"]).replace(tzinfo=timezone.utc))
        out.parent.mkdir(parents=True, exist_ok=True)
        problems = package.pack(tmp, out, original=template)
        if problems:
            log("ERROR: the deck's package is broken, nothing written:\n  " + "\n  ".join(problems[:20]))
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
