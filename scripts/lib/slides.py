"""Find and read the editable structures on each kind of deck slide.

Structures are located by what they ARE (geometry and content), not by shape ids or by slide numbers,
so the same code reads the reference deck (to cut the template and seed curated wording), the template
(to render) and a built deck (to verify):

  * month-column slides — three header shapes above three tall body boxes; each body lists entries
    (a hyperlinked heading paragraph such as ``v1.16.5 (2026-09-30)`` or ``2026-10-02``, then its
    highlight paragraphs);
  * blog slides — eight grouped entry boxes (date + "See here" link, then the post title);
  * webinar slides — event cards (``NOV 4, 2026 | WEBINAR | See here`` / title / description);
  * featured-event slides — title, ``Event | <date> | <links>`` line, description, pictures.

Rich text travels as a small markup: ``**bold**`` marks a bold phrase; everything else is plain.
"""
from __future__ import annotations

import re

from lib import pptx_edit as px

EMU_PT = 12700
EMU_IN = 914400
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
SLOT_HEAD_RE = re.compile(r"^\s*[A-Z]{3,9}\.? \d{1,2}, \d{4}\s*\|")
SEE_TOKENS = {"see", "here", "see here"}


# --------------------------------------------------------------------------- geometry -------------
def box(node) -> tuple[int, int, int, int] | None:
    """(x, y, cx, cy) in EMU from a shape / picture / group's own transform, or None."""
    for xf in node.getElementsByTagName("a:xfrm"):
        # the node's OWN transform is the first one that is not inside a nested child shape
        holder = xf.parentNode
        if holder.parentNode is not node:
            continue
        off = [c for c in xf.childNodes if c.nodeName == "a:off"]
        ext = [c for c in xf.childNodes if c.nodeName == "a:ext"]
        if off and ext:
            return (int(off[0].getAttribute("x")), int(off[0].getAttribute("y")),
                    int(ext[0].getAttribute("cx")), int(ext[0].getAttribute("cy")))
    return None


def set_box(node, x=None, y=None, cx=None, cy=None) -> None:
    """Update the node's own transform; None leaves a value unchanged."""
    for xf in node.getElementsByTagName("a:xfrm"):
        if xf.parentNode.parentNode is not node:
            continue
        for child in xf.childNodes:
            if child.nodeName == "a:off":
                if x is not None:
                    child.setAttribute("x", str(int(x)))
                if y is not None:
                    child.setAttribute("y", str(int(y)))
            elif child.nodeName == "a:ext":
                if cx is not None:
                    child.setAttribute("cx", str(int(cx)))
                if cy is not None:
                    child.setAttribute("cy", str(int(cy)))
        return


def sp_tree(dom):
    return dom.getElementsByTagName("p:spTree")[0]


def top_level(dom, *tags: str) -> list:
    """Direct children of the slide's shape tree with one of the given tag names."""
    return [n for n in sp_tree(dom).childNodes if n.nodeName in tags]


def centre_inside(inner, outer) -> bool:
    cx, cy = inner[0] + inner[2] // 2, inner[1] + inner[3] // 2
    return outer[0] <= cx <= outer[0] + outer[2] and outer[1] <= cy <= outer[1] + outer[3]


# --------------------------------------------------------------------------- text -----------------
def para_text(p) -> str:
    return "".join(px.run_text(r) for r in px.para_runs(p))


def is_bold(run) -> bool:
    rpr = run.getElementsByTagName("a:rPr")
    return bool(rpr) and rpr[0].getAttribute("b") == "1"


def to_markup(p) -> str:
    """A paragraph's text with its bold phrases wrapped in ``**`` (adjacent bold runs merged)."""
    out, bold_open = [], False
    for run in px.para_runs(p):
        text = px.run_text(run)
        if not text:
            continue
        bold = is_bold(run) and bool(text.strip())
        if bold != bold_open:
            out.append("**")
            bold_open = bold
        out.append(text)
    if bold_open:
        out.append("**")
    return tidy_markup("".join(out))


def tidy_markup(text: str) -> str:
    """Normalize markup: markers hug their phrase, empty/whitespace-only bold spans vanish."""
    parts = text.split("**")
    out = []
    for i, part in enumerate(parts):
        if i % 2 == 1:  # a bold span
            lead = part[:len(part) - len(part.lstrip())]
            trail = part[len(part.rstrip()):]
            core = part.strip()
            out.append(f"{lead}**{core}**{trail}" if core else part)
        else:
            out.append(part)
    return re.sub(r"\s+", " ", "".join(out)).strip()


def strip_markup(text: str) -> str:
    return (text or "").replace("**", "")


def split_markup(text: str) -> list[tuple[str, bool]]:
    """``"a **b** c"`` -> [("a ", False), ("b", True), (" c", False)]."""
    return [(part, i % 2 == 1) for i, part in enumerate((text or "").split("**")) if part]


def link_target(run, rid_target: dict) -> str | None:
    hl = px.run_hyperlink(run)
    return rid_target.get(hl.getAttribute("r:id")) if hl is not None else None


# --------------------------------------------------------------------------- month columns --------
def columns(dom) -> list[dict]:
    """The slide's month columns, left to right: ``[{"header": sp, "body": sp}]``.

    A body is a tall text box in the lower two thirds of the slide; its header is the short shape
    directly above it. Returns [] when the slide does not have that structure."""
    shapes = [(sp, box(sp)) for sp in top_level(dom, "p:sp")]
    shapes = [(sp, b) for sp, b in shapes if b and sp.getElementsByTagName("p:txBody")]
    bodies = [(sp, b) for sp, b in shapes
              if b[3] >= 1.5 * EMU_IN and 1.5 * EMU_IN <= b[2] <= 4.5 * EMU_IN and b[1] >= 1.2 * EMU_IN]
    out = []
    for body, bb in sorted(bodies, key=lambda t: t[1][0]):
        above = [(sp, b) for sp, b in shapes
                 if sp is not body and b[3] < 0.6 * EMU_IN and b[1] + b[3] <= bb[1] + EMU_PT * 6
                 and bb[1] - (b[1] + b[3]) < 0.6 * EMU_IN
                 and abs((b[0] + b[2] / 2) - (bb[0] + bb[2] / 2)) < bb[2] / 2]
        if not above:
            continue
        header = max(above, key=lambda t: t[1][1])[0]
        out.append({"header": header, "body": body})
    return out


def is_heading(p) -> bool:
    """An entry heading: a paragraph that STARTS with a hyperlinked run naming a version or a date.
    (A highlight may contain a link too — "Refer to the Vault 2.0.4 release notes" — but never
    starts with one that is a version/date.)"""
    for run in px.para_runs(p):
        text = px.run_text(run)
        if not text.strip():
            continue
        if px.run_hyperlink(run) is None:
            return False
        full = para_text(p)
        return bool(VERSION_RE.search(full) or ISO_DATE_RE.search(full))
    return False


def read_entries(body_sp, rid_target: dict) -> list[dict]:
    """A column body's entries, top to bottom:
    ``{"heading", "versions", "date", "links", "bullets"}`` (bullets as markup)."""
    entries, current = [], None
    for p in px.paragraphs(body_sp):
        text = para_text(p)
        if not text.strip():
            continue
        if is_heading(p):
            links = [t for t in (link_target(r, rid_target) for r in px.para_runs(p)) if t]
            date = ISO_DATE_RE.search(text)
            current = {"heading": " ".join(text.split()), "versions": VERSION_RE.findall(text),
                       "date": date.group(0) if date else None,
                       "links": list(dict.fromkeys(links)), "bullets": []}
            entries.append(current)
        elif current is not None:
            current["bullets"].append(to_markup(p))
    return entries


def header_text(header_sp) -> str:
    return " ".join(px.shape_text(header_sp).split())


# --------------------------------------------------------------------------- blogs ----------------
def _see_runs(sp) -> list:
    return [r for r in sp.getElementsByTagName("a:r")
            if px.run_hyperlink(r) is not None and px.run_text(r).strip().lower() in SEE_TOKENS]


def blog_groups(dom, slide_cx: int) -> list[dict]:
    """The blog entry boxes in reading order (left column top-down, then right column top-down):
    ``[{"group": grpSp, "box": sp}]``. `box` is the text box inside the group."""
    out = []
    for grp in top_level(dom, "p:grpSp"):
        gb = box(grp)
        boxes = [sp for sp in grp.getElementsByTagName("p:sp") if _see_runs(sp)]
        if gb and len(boxes) == 1:
            out.append({"group": grp, "box": boxes[0], "geom": gb})
    out.sort(key=lambda g: (0 if g["geom"][0] + g["geom"][2] / 2 < slide_cx / 2 else 1, g["geom"][1]))
    return out


def read_blog_entry(box_sp, rid_target: dict) -> dict:
    """``{"date_display", "title" (markup), "url"}`` of a blog entry box."""
    paras = px.paragraphs(box_sp)
    see = _see_runs(box_sp)
    first_runs = px.para_runs(paras[0]) if paras else []
    date_run = px.run_text(first_runs[0]) if first_runs else ""
    date_display = re.sub(r"\s*[–—-]\s*$", "", date_run).strip()
    title = " ".join(to_markup(p) for p in paras[1:] if para_text(p).strip())
    return {"date_display": date_display, "title": title.strip(),
            "url": link_target(see[-1], rid_target) if see else None}


# --------------------------------------------------------------------------- webinars -------------
def webinar_slots(dom) -> list:
    """Event cards, left to right then top to bottom: text boxes whose first line reads
    ``<DATE> | <TYPE> | See here``."""
    slots = []
    for sp in top_level(dom, "p:sp"):
        paras = px.paragraphs(sp)
        if len(paras) >= 2 and SLOT_HEAD_RE.match(para_text(paras[0])) and box(sp):
            slots.append(sp)
    return sorted(slots, key=lambda sp: (round(box(sp)[1] / (EMU_IN / 2)), box(sp)[0]))


def read_slot(slot_sp, rid_target: dict) -> dict:
    paras = [p for p in px.paragraphs(slot_sp)]
    head = para_text(paras[0])
    bits = [b.strip() for b in head.split("|")]
    url = next((t for t in (link_target(r, rid_target) for r in px.para_runs(paras[0])) if t), None)
    rest = [para_text(p).strip() for p in paras[1:]]
    rest = [t for t in rest if t]
    return {"date_label": bits[0] if bits else "", "kind": bits[1] if len(bits) > 1 else "",
            "url": url, "title": rest[0] if rest else "", "description": " ".join(rest[1:])}


def slot_decorations(dom, slot_sp) -> list:
    """Pictures and groups that sit on a card (its flag and product icons)."""
    sb = box(slot_sp)
    out = []
    for node in top_level(dom, "p:pic", "p:grpSp"):
        nb = box(node)
        if nb and nb[2] < EMU_IN and nb[3] < EMU_IN and centre_inside(nb, sb):
            out.append(node)
    return out


def is_flag(pic) -> bool:
    """A language flag: a small picture drawn as a circle, or one whose alt text names a flag."""
    if pic.nodeName != "p:pic":
        return False
    if any(g.getAttribute("prst") == "ellipse" for g in pic.getElementsByTagName("a:prstGeom")):
        return True
    cnv = pic.getElementsByTagName("p:cNvPr")
    descr = (cnv[0].getAttribute("descr") if cnv else "").lower()
    return "flag" in descr or "union jack" in descr


# --------------------------------------------------------------------------- featured event -------
def featured_parts(dom) -> dict:
    """The featured slide's text shapes by role: ``{"title", "subtitle", "description"}`` (any may be
    missing). The subtitle is the ``Event | <date> | <links>`` line; the title sits above it; the
    description is the remaining text block with the most text."""
    shapes = [sp for sp in top_level(dom, "p:sp") if box(sp) and px.shape_text(sp).strip()]
    subtitle = next((sp for sp in shapes if re.match(r"\s*Event\s*\|", px.shape_text(sp))), None)
    parts = {"subtitle": subtitle}
    rest = [sp for sp in shapes if sp is not subtitle]
    if subtitle is not None:
        above = [sp for sp in rest if box(sp)[1] < box(subtitle)[1]]
        parts["title"] = max(above, key=lambda sp: box(sp)[1]) if above else None
    else:
        parts["title"] = next((sp for sp in rest if px.shape_name(sp).startswith("Title")), None)
    rest = [sp for sp in rest if sp is not parts.get("title")]
    parts["description"] = max(rest, key=lambda sp: len(px.shape_text(sp))) if rest else None
    return parts


def read_featured(dom, rid_target: dict) -> dict:
    parts = featured_parts(dom)
    out = {"title": "", "date_display": "", "links": [], "description_points": []}
    if parts.get("title") is not None:
        out["title"] = " ".join(px.shape_text(parts["title"]).split())
    if parts.get("subtitle") is not None:
        p0 = px.paragraphs(parts["subtitle"])[0]
        bits = [b.strip() for b in para_text(p0).split("|")]
        out["date_display"] = bits[1] if len(bits) > 1 else ""
        for run in px.para_runs(p0):
            target = link_target(run, rid_target)
            if target:
                out["links"].append({"label": px.run_text(run).strip(), "url": target})
    if parts.get("description") is not None:
        out["description_points"] = [to_markup(p) for p in px.paragraphs(parts["description"])
                                     if para_text(p).strip()]
    return out
