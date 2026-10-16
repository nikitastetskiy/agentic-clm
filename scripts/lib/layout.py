"""Keep the repeated elements of a slide aligned, everything on the slide, and highlights apart.

A hand-made deck drifts: three event cards meant to sit on one line are 2 pt apart, a flag sits 2 pt
higher on one card than on the next, a Vault icon floats 4 pt above the Terraform icon beside it, a
month header is 0.8 pt off its neighbours, a background picture runs 8 pt past the slide's bottom
edge. Each slip is small alone and obvious side by side, and the template inherits all of them from
the reference deck. So every build re-establishes the layout the template MEANS (the ``align_*`` /
``place_*`` functions, called by build_deck.py), and the gate checks the same rules on the result
(the ``*_problems`` functions, called by verify_build.py). One module holds both, so they cannot
disagree.

The rules (the card values follow a reviewer's corrections of a built deck, 2026-10-14):
  * nothing extends past the slide's edges — a picture that does is cropped, never squashed;
  * event cards in a row share one top and one size, and are evenly spaced;
  * a card's flag sits a fixed distance from its top-right corner;
  * a card's product icons share one top line near its bottom-left corner, left to right, separated
    by the same 13.5 pt as the first icon's distance from the card's edge;
  * month columns: headers on one top line, bodies on one top line, and two highlights of the same
    entry never run into each other (at least MIN_HIGHLIGHT_GAP_PT between them);
  * blog entries: one x per column, the left column's rows evenly spaced, the right column's rows
    on the left column's.
"""
from __future__ import annotations

from collections import Counter

from lib import pptx_edit as px
from lib import slides as sl

EMU_PT = 12700
CARD_FLAG_TOP = 48577        # 3.8 pt from a card's top edge to its flag (as on the Vault webinar cards)
CARD_FLAG_RIGHT = 67814      # 5.3 pt from a card's right edge to its flag
CARD_ICON_LEFT = 171450      # 13.5 pt from a card's left edge to its first icon
CARD_ICON_GAP = 171450       # 13.5 pt between two icons: the same as their inset (reviewer's correction)
CARD_ICON_TOP = 401320       # 31.6 pt above a card's bottom edge: the icons' common top line
CARD_ICON_ROW = 444500       # 35 pt at the bottom of a card that its text must leave free for the icons
MIN_HIGHLIGHT_GAP_PT = 3.0   # least space between two highlights of one entry (the "tight" spacing)
TOLERANCE = 2540             # 0.2 pt: how far off a line the gate still accepts


def _pt(emu: float) -> str:
    return f"{emu / EMU_PT:.1f} pt"


def _name(node) -> str:
    cnv = node.getElementsByTagName("p:cNvPr")
    return cnv[0].getAttribute("name") if cnv else node.nodeName


def _pin(nodes: list, **values) -> None:
    for node in nodes:
        sl.set_box(node, **values)


def spread(nodes: list, axis: int) -> None:
    """Space `nodes` evenly along x (axis 0) or y (axis 1) between the first and the last one."""
    if len(nodes) < 3:
        return
    nodes = sorted(nodes, key=lambda n: sl.box(n)[axis])
    first, last = sl.box(nodes[0])[axis], sl.box(nodes[-1])[axis]
    step = (last - first) / (len(nodes) - 1)
    for i, node in enumerate(nodes[1:-1], 1):
        sl.set_box(node, **{"xy"[axis]: round(first + i * step)})


def _uneven(values: list[int]) -> bool:
    if len(values) < 3:
        return False
    values = sorted(values)
    steps = [b - a for a, b in zip(values, values[1:])]
    return max(steps) - min(steps) > 2 * TOLERANCE


# --------------------------------------------------------------------------- slide bounds ---------
def _outside(box, slide_cx: int, slide_cy: int) -> bool:
    x, y, w, h = box
    return x < -TOLERANCE or y < -TOLERANCE or x + w > slide_cx + TOLERANCE or y + h > slide_cy + TOLERANCE


def keep_inside(dom, slide_cx: int, slide_cy: int) -> list[str]:
    """Crop every picture that extends past the slide's edges to the part that is on the slide: the
    picture keeps its proportions, the hidden part is cut off with a source crop. Returns what was
    cropped, for the build report."""
    cropped = []
    for pic in sl.top_level(dom, "p:pic"):
        box = sl.box(pic)
        if not box or box[2] <= 0 or box[3] <= 0 or not _outside(box, slide_cx, slide_cy):
            continue
        x, y, w, h = box
        cut = {"l": max(0, -x) / w, "t": max(0, -y) / h,
               "r": max(0, x + w - slide_cx) / w, "b": max(0, y + h - slide_cy) / h}
        _crop(pic, cut)
        sl.set_box(pic, x=max(x, 0), y=max(y, 0),
                   cx=round(w * (1 - cut["l"] - cut["r"])), cy=round(h * (1 - cut["t"] - cut["b"])))
        cropped.append(f"{_name(pic)}: cropped to the slide")
    return cropped


def _crop(pic, cut: dict) -> None:
    """Extend the picture's source crop by `cut` (fractions of its current box per side)."""
    fill = pic.getElementsByTagName("p:blipFill")[0]
    rects = [c for c in fill.childNodes if c.nodeName == "a:srcRect"]
    if rects:
        rect = rects[0]
    else:
        rect = pic.ownerDocument.createElement("a:srcRect")
        blip = [c for c in fill.childNodes if c.nodeName == "a:blip"][0]
        fill.insertBefore(rect, blip.nextSibling)       # order: blip, srcRect, stretch
    old = {k: int(rect.getAttribute(k) or 0) / 100000 for k in "ltrb"}
    for first, second in (("l", "r"), ("t", "b")):
        span = 1 - old[first] - old[second]             # share of the source the box shows now
        for side in (first, second):
            value = old[side] + cut[side] * span
            if value:
                rect.setAttribute(side, str(round(value * 100000)))


def bounds_problems(dom, slide_cx: int, slide_cy: int) -> list[str]:
    out = []
    for node in sl.top_level(dom, "p:sp", "p:pic", "p:grpSp", "p:graphicFrame", "p:cxnSp"):
        box = sl.box(node)
        if box and _outside(box, slide_cx, slide_cy):
            x, y, w, h = box
            out.append(f"'{_name(node)}' extends past the slide's edges (left {_pt(x)}, top {_pt(y)}, "
                       f"right {_pt(x + w)}, bottom {_pt(y + h)} on a {_pt(slide_cx)} x {_pt(slide_cy)} slide)")
    return out


# --------------------------------------------------------------------------- event cards ----------
def card_rows(slots: list) -> list[list]:
    """Cards grouped into rows (cards whose tops are less than half a card apart), each left to right."""
    rows: list[list] = []
    for slot in sorted(slots, key=lambda s: sl.box(s)[1]):
        if rows and abs(sl.box(slot)[1] - sl.box(rows[-1][0])[1]) < sl.box(rows[-1][0])[3] / 2:
            rows[-1].append(slot)
        else:
            rows.append([slot])
    return [sorted(row, key=lambda s: sl.box(s)[0]) for row in rows]


def align_cards(slots: list) -> None:
    """Every card of a row takes the row's first card's top and size; the row is evenly spaced."""
    for row in card_rows(slots):
        _x, y, cx, cy = sl.box(row[0])
        _pin(row[1:], y=y, cx=cx, cy=cy)
        spread(row, 0)


def place_flag(flag, card) -> None:
    x, y, cx, _cy = sl.box(card)
    sl.set_box(flag, x=x + cx - CARD_FLAG_RIGHT - sl.box(flag)[2], y=y + CARD_FLAG_TOP)


def place_icons(icons: list, card) -> None:
    """Icons left to right from the card's bottom-left corner, all on one top line."""
    x, y, _cx, cy = sl.box(card)
    cursor = x + CARD_ICON_LEFT
    for icon in icons:
        sl.set_box(icon, x=cursor, y=y + cy - CARD_ICON_TOP)
        cursor += sl.box(icon)[2] + CARD_ICON_GAP


def card_problems(dom) -> list[str]:
    slots = sl.webinar_slots(dom)
    out = []
    for row in card_rows(slots):
        first = sl.box(row[0])
        for slot in row[1:]:
            b = sl.box(slot)
            if any(abs(b[k] - first[k]) > TOLERANCE for k in (1, 2, 3)):
                out.append(f"event cards are not on one line or not one size (top {_pt(first[1])} / "
                           f"{_pt(b[1])}, size {_pt(first[2])} x {_pt(first[3])} / {_pt(b[2])} x {_pt(b[3])})")
        if _uneven([sl.box(s)[0] for s in row]):
            out.append("event cards are not evenly spaced")
    for i, slot in enumerate(slots, 1):
        x, y, cx, cy = sl.box(slot)
        title = sl.read_slot(slot, {})["title"][:40]
        decorations = sl.slot_decorations(dom, slot)
        for flag in [d for d in decorations if sl.is_flag(d)]:
            fx, fy, fw, _fh = sl.box(flag)
            if abs(fy - y - CARD_FLAG_TOP) > TOLERANCE or abs(x + cx - fx - fw - CARD_FLAG_RIGHT) > TOLERANCE:
                out.append(f"card {i} ({title}): flag is {_pt(fy - y)} from the top and "
                           f"{_pt(x + cx - fx - fw)} from the right edge, not {_pt(CARD_FLAG_TOP)} / "
                           f"{_pt(CARD_FLAG_RIGHT)}")
        icons = sorted((d for d in decorations if not sl.is_flag(d)), key=lambda d: sl.box(d)[0])
        cursor = x + CARD_ICON_LEFT
        for icon in icons:
            ix, iy, iw, _ih = sl.box(icon)
            if abs(iy - (y + cy - CARD_ICON_TOP)) > TOLERANCE:
                out.append(f"card {i} ({title}): icon '{_name(icon)}' is off the icons' top line by "
                           f"{_pt(iy - (y + cy - CARD_ICON_TOP))}")
            if abs(ix - cursor) > TOLERANCE:
                out.append(f"card {i} ({title}): icon '{_name(icon)}' is {_pt(ix - cursor)} off its place "
                           f"({_pt(CARD_ICON_LEFT)} from the edge, {_pt(CARD_ICON_GAP)} between icons)")
            cursor = ix + iw + CARD_ICON_GAP
    return out


# --------------------------------------------------------------------------- month columns --------
def align_columns(cols: list[dict]) -> None:
    """Month columns: the headers on the first header's top line, the bodies on the first body's."""
    if cols:
        _pin([c["header"] for c in cols[1:]], y=sl.box(cols[0]["header"])[1])
        _pin([c["body"] for c in cols[1:]], y=sl.box(cols[0]["body"])[1])


def highlight_gap_pt(heading_after_pt: float, bullet_after_pt: float) -> float:
    """Space-before for a highlight that follows another highlight of the same entry: as far apart as
    a heading is from its first highlight, and never less than MIN_HIGHLIGHT_GAP_PT. (The reference
    deck's authors typed an empty line between such highlights; a stored spacing does the same job
    without an empty bulleted paragraph.)"""
    return max(0.0, max(heading_after_pt, MIN_HIGHLIGHT_GAP_PT) - bullet_after_pt)


def column_problems(cols: list[dict], style_of) -> list[str]:
    """`style_of(p)` -> textfit.ParaStyle (passed in, so this module does not depend on compose)."""
    out = []
    if not cols:
        return out
    for part in ("header", "body"):
        tops = [sl.box(c[part])[1] for c in cols]
        if max(tops) - min(tops) > TOLERANCE:
            out.append(f"month {part}s are not on one line (tops {', '.join(_pt(t) for t in tops)})")
    for col in cols:
        prev = None
        for p in px.paragraphs(col["body"]):
            if not sl.para_text(p).strip():
                continue
            heading = sl.is_heading(p)
            if prev is not None and not heading and not sl.is_heading(prev):
                gap = style_of(prev).after_pt + style_of(p).before_pt
                if gap < MIN_HIGHLIGHT_GAP_PT - 0.01:
                    out.append(f"{sl.header_text(col['header'])}: two highlights run together ({gap:.0f} pt "
                               f"apart, at least {MIN_HIGHLIGHT_GAP_PT:.0f}): "
                               f"'{sl.para_text(prev)[:40]}…' / '{sl.para_text(p)[:40]}…'")
            prev = p
    return out


# --------------------------------------------------------------------------- blog grid ------------
def blog_columns(groups: list[dict], slide_cx: int) -> tuple[list, list]:
    left = [g["group"] for g in groups if sl.box(g["group"])[0] + sl.box(g["group"])[2] / 2 < slide_cx / 2]
    right = [g["group"] for g in groups if g["group"] not in left]
    return left, right


def align_blog_grid(groups: list[dict], slide_cx: int) -> None:
    """Blog entries: one x per column (the one most entries already use), the left column's rows
    evenly spaced, each right-column entry on the nearest left-column row."""
    left, right = blog_columns(groups, slide_cx)
    for column in (left, right):
        if column:
            _pin(column, x=Counter(sl.box(g)[0] for g in column).most_common(1)[0][0])
    spread(left, 1)
    rows = [sl.box(g)[1] for g in left]
    for group in right:
        if rows:
            y = sl.box(group)[1]
            sl.set_box(group, y=min(rows, key=lambda r: abs(r - y)))


def blog_problems(groups: list[dict], slide_cx: int) -> list[str]:
    out = []
    left, right = blog_columns(groups, slide_cx)
    for name, column in (("left", left), ("right", right)):
        xs = [sl.box(g)[0] for g in column]
        if xs and max(xs) - min(xs) > TOLERANCE:
            out.append(f"blog entries of the {name} column are not on one x ({', '.join(_pt(x) for x in xs)})")
    rows = [sl.box(g)[1] for g in left]
    if _uneven(rows):
        out.append(f"blog rows are not evenly spaced ({', '.join(_pt(r) for r in sorted(rows))})")
    for group in right:
        y = sl.box(group)[1]
        if rows and min(abs(y - r) for r in rows) > TOLERANCE:
            out.append(f"a right-column blog entry ({_pt(y)}) is not on a left-column row")
    return out
