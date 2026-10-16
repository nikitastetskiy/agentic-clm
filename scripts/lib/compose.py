"""Compose slide text from PROTOTYPE paragraphs taken from the template.

The builder never invents formatting. Every paragraph it writes is a clone of a paragraph the template
already has (an entry heading, a highlight bullet, a card description), with only its runs replaced —
so fonts, sizes, colours, bullets and spacing are whatever the reference deck's author chose, including
the per-product accent colours (Terraform purple, Vault orange) that differ from slide to slide.
"""
from __future__ import annotations

from lib import pptx_edit as px
from lib.textfit import DEFAULT_INSET, EMU_PT, ParaStyle

DEFAULT_INSET_TB = 45720   # a text box's default top/bottom inset (0.05 in), in EMU
RUN_LIKE = ("a:r", "a:br", "a:fld")
# <a:rPr> children that must come AFTER a hyperlink element (schema order).
AFTER_HLINK = ("a:hlinkMouseOver", "a:rtl", "a:extLst")


def insets(sp) -> tuple[int, int, int, int]:
    """(left, top, right, bottom) text insets of a shape, in EMU."""
    body = sp.getElementsByTagName("a:bodyPr")
    def val(name: str, default: int) -> int:
        raw = body[0].getAttribute(name) if body else ""
        return int(raw) if raw else default
    return (val("lIns", DEFAULT_INSET), val("tIns", DEFAULT_INSET_TB),
            val("rIns", DEFAULT_INSET), val("bIns", DEFAULT_INSET_TB))


def _own(node, tag: str):
    """`node`'s direct child elements named `tag`."""
    return [c for c in node.childNodes if c.nodeName == tag]


def style_of(p, *, default_size: float = 8.0, shape=None) -> ParaStyle:
    """The layout-relevant formatting of a paragraph (see textfit.ParaStyle). Values the paragraph
    does not set itself are taken from `shape`'s level-1 list style when one is given (a placeholder
    usually keeps its bullet indent and paragraph spacing there)."""
    size, bold = default_size, False
    runs = _own(p, "a:r")
    props = (runs[0].getElementsByTagName("a:rPr") if runs else []) or _own(p, "a:endParaRPr")
    if props:
        if props[0].getAttribute("sz"):
            size = int(props[0].getAttribute("sz")) / 100
        bold = props[0].getAttribute("b") == "1"
    found: dict[str, float] = {}
    levels = shape.getElementsByTagName("a:lvl1pPr") if shape is not None else []
    for ppr in _own(p, "a:pPr") + list(levels[:1]):   # the paragraph's own values win
        if ppr.getAttribute("marL"):
            found.setdefault("indent", int(ppr.getAttribute("marL")) / EMU_PT)
        for name, key in (("a:lnSpc", "line"), ("a:spcBef", "before"), ("a:spcAft", "after")):
            for el in _own(ppr, name):
                pct = el.getElementsByTagName("a:spcPct")
                pts = el.getElementsByTagName("a:spcPts")
                if key == "line" and pct:
                    found.setdefault(key, int(pct[0].getAttribute("val")) / 100000)
                elif key != "line" and pts:
                    found.setdefault(key, int(pts[0].getAttribute("val")) / 100)
    return ParaStyle(size_pt=size, bold=bold, line_pct=found.get("line", 1.0),
                     before_pt=found.get("before", 0.0), after_pt=found.get("after", 0.0),
                     indent_pt=found.get("indent", 0.0))


def set_spacing(p, mode: str) -> None:
    """Apply one of textfit's spacing modes to a paragraph (see textfit.spaced)."""
    if mode == "template":
        return
    for ppr in _own(p, "a:pPr"):
        for el in _own(ppr, "a:lnSpc"):
            ppr.removeChild(el)
        if mode == "tight":
            for name in ("a:spcBef", "a:spcAft"):
                for el in _own(ppr, name):
                    for pts in el.getElementsByTagName("a:spcPts"):
                        if int(pts.getAttribute("val")) > 300:
                            pts.setAttribute("val", "300")


def set_space_before(p, pt: float) -> None:
    """Store `pt` as the paragraph's space-before (replacing any it has)."""
    pprs = _own(p, "a:pPr")
    if pprs:
        ppr = pprs[0]
    else:
        ppr = p.ownerDocument.createElement("a:pPr")
        p.insertBefore(ppr, p.firstChild)
    for el in _own(ppr, "a:spcBef"):
        ppr.removeChild(el)
    bef = p.ownerDocument.createElement("a:spcBef")
    pts = p.ownerDocument.createElement("a:spcPts")
    pts.setAttribute("val", str(round(pt * 100)))
    bef.appendChild(pts)
    # schema order inside a:pPr: lnSpc, spcBef, spcAft, bullet properties, …
    ref = next((c for c in ppr.childNodes if c.nodeType == c.ELEMENT_NODE and c.nodeName != "a:lnSpc"), None)
    ppr.insertBefore(bef, ref)


def empty_clone(proto):
    """A copy of a prototype paragraph with its runs removed (formatting kept). A hyperlink left on
    the paragraph's end mark would make the whole line clickable, so it is dropped."""
    p = proto.cloneNode(True)
    for child in list(p.childNodes):
        if child.nodeName in RUN_LIKE:
            p.removeChild(child)
    for end in _own(p, "a:endParaRPr"):
        for hl in end.getElementsByTagName("a:hlinkClick"):
            hl.parentNode.removeChild(hl)
    return p


def first_run(p, *, linked: bool | None = None):
    """The paragraph's first run with text; `linked` narrows it to a (non-)hyperlinked run."""
    for run in _own(p, "a:r"):
        if not px.run_text(run).strip():
            continue
        if linked is None or (px.run_hyperlink(run) is not None) == linked:
            return run
    return None


def add_run(p, proto_run, text: str, *, bold: bool | None = None, url: str | None = None,
            rels_dom=None) -> None:
    """Append a run cloned from `proto_run`. `bold` True/False forces the weight (None keeps the
    prototype's); `url` makes it a hyperlink through a FRESH relationship, and a run without `url`
    is never left carrying the prototype's link."""
    run = proto_run.cloneNode(True)
    px.set_run_text(run, text)
    rprs = run.getElementsByTagName("a:rPr")
    if rprs:
        rpr = rprs[0]
    else:
        rpr = run.ownerDocument.createElement("a:rPr")
        rpr.setAttribute("lang", "en-US")
        run.insertBefore(rpr, run.firstChild)
    for attr in ("err", "dirty"):
        if rpr.hasAttribute(attr):
            rpr.removeAttribute(attr)
    if bold is True:
        rpr.setAttribute("b", "1")
    elif bold is False and rpr.hasAttribute("b"):
        rpr.removeAttribute("b")
    hl = px.run_hyperlink(run)
    if url:
        if hl is None:
            hl = run.ownerDocument.createElement("a:hlinkClick")
            ref = next((c for c in rpr.childNodes if c.nodeName in AFTER_HLINK), None)
            rpr.insertBefore(hl, ref)
        hl.setAttribute("r:id", px.add_rel(rels_dom, url))
    elif hl is not None:
        hl.parentNode.removeChild(hl)
    ends = _own(p, "a:endParaRPr")
    p.insertBefore(run, ends[0] if ends else None)


def paragraph(proto, segments: list[dict], rels_dom=None, *, spacing: str = "template"):
    """A new paragraph in `proto`'s formatting. Each segment is ``{"text", "bold"?, "url"?}``.
    Linked segments clone the prototype's hyperlinked run when it has one, the others its first
    plain run, so each keeps the look of its kind."""
    plain = first_run(proto, linked=False) or first_run(proto)
    linked = first_run(proto, linked=True) or plain
    p = empty_clone(proto)
    for seg in segments:
        if not seg.get("text"):
            continue
        add_run(p, linked if seg.get("url") else plain, seg["text"], bold=seg.get("bold"),
                url=seg.get("url"), rels_dom=rels_dom)
    set_spacing(p, spacing)
    return p


def markup_segments(markup: str) -> list[dict]:
    """``"a **b** c"`` -> segments with explicit weights (see lib.slides.split_markup)."""
    return [{"text": part, "bold": i % 2 == 1} for i, part in enumerate((markup or "").split("**")) if part]


def replace_paragraphs(sp, paragraphs: list) -> None:
    """Swap a shape's paragraphs for `paragraphs` (at least one must be given)."""
    body = sp.getElementsByTagName("p:txBody")[0]
    for p in _own(body, "a:p"):
        body.removeChild(p)
    for p in paragraphs:
        body.appendChild(p)


def set_text(p, text: str) -> bool:
    """Put `text` into the paragraph's first run and drop its other runs. False if it has no run."""
    runs = _own(p, "a:r")
    if not runs:
        return False
    px.set_run_text(runs[0], text)
    for child in list(p.childNodes):
        if child.nodeName in RUN_LIKE and child is not runs[0]:
            p.removeChild(child)
    return True
