"""Small helpers for editing a slide's XML: its text runs, its relationships, its pictures.

Two rules the helpers encode:

  * NEVER mutate an existing relationship's Target — rIds are frequently SHARED across entries, so
    repointing one corrupts another. Always mint a FRESH relationship and repoint the run or picture.
  * Edit text INSIDE existing `<a:t>` runs (preserving every `<a:rPr>`); to shorten content, blank
    or remove trailing runs/paragraphs rather than rebuilding a shape from scratch.
"""
from __future__ import annotations

import shutil
from pathlib import Path
from xml.dom import minidom

from lib.package import REL_NS

HYPERLINK_TYPE = REL_NS + "/hyperlink"
IMAGE_TYPE = REL_NS + "/image"
SLIDE_TYPE = REL_NS + "/slide"
# Stray invisible characters that hide in hand-authored runs (a U+200B zero-width space in the
# reference deck's "All global events here"): they render as nothing and break text matching.
_INVISIBLES = dict.fromkeys([0x200B, 0x200C, 0x200D, 0xFEFF, 0x00AD, 0x2060], None)
_PICTURE = (
    '<p:pic xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:r="' + REL_NS + '">'
    '<p:nvPicPr><p:cNvPr id="0" name=""/><p:cNvPicPr><a:picLocks noChangeAspect="1"/></p:cNvPicPr>'
    '<p:nvPr/></p:nvPicPr><p:blipFill><a:blip r:embed=""/><a:stretch><a:fillRect/></a:stretch></p:blipFill>'
    '<p:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="0" cy="0"/></a:xfrm>'
    '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr></p:pic>')


# --------------------------------------------------------------------------- text -----------------
def shape_name(sp) -> str:
    nv = sp.getElementsByTagName("p:cNvPr")
    return nv[0].getAttribute("name") if nv else ""


def shape_text(sp) -> str:
    return "".join(t.firstChild.data for t in sp.getElementsByTagName("a:t") if t.firstChild)


def run_text(run) -> str:
    ats = run.getElementsByTagName("a:t")
    return ats[0].firstChild.data if ats and ats[0].firstChild else ""


def set_run_text(run, text: str) -> None:
    """Set a run's visible text, preserving its <a:rPr>. Creates the <a:t> if missing."""
    ats = run.getElementsByTagName("a:t")
    if ats:
        if ats[0].firstChild:
            ats[0].firstChild.data = text
        else:
            ats[0].appendChild(run.ownerDocument.createTextNode(text))
    else:
        at = run.ownerDocument.createElement("a:t")
        at.appendChild(run.ownerDocument.createTextNode(text))
        run.appendChild(at)


def run_hyperlink(run):
    hl = run.getElementsByTagName("a:hlinkClick")
    return hl[0] if hl else None


def strip_invisibles(dom) -> None:
    """Remove stray zero-width characters from every text run of the slide."""
    for t in dom.getElementsByTagName("a:t"):
        if t.firstChild is not None and t.firstChild.data:
            t.firstChild.data = t.firstChild.data.translate(_INVISIBLES)


# --------------------------------------------------------------------------- relationships --------
def load_rels(rels_file: Path) -> tuple[minidom.Document, dict]:
    dom = minidom.parse(str(rels_file))
    rid_target = {r.getAttribute("Id"): r.getAttribute("Target")
                  for r in dom.getElementsByTagName("Relationship")}
    return dom, rid_target


def add_relationship(rels_dom, rel_type: str, target: str, *, external: bool = False) -> str:
    """Append a NEW relationship and return its id (never repoint a shared one)."""
    nums = [int(r.getAttribute("Id")[3:]) for r in rels_dom.getElementsByTagName("Relationship")
            if r.getAttribute("Id").startswith("rId") and r.getAttribute("Id")[3:].isdigit()]
    rel = rels_dom.createElement("Relationship")
    rel.setAttribute("Id", f"rId{max(nums, default=0) + 1}")
    rel.setAttribute("Type", rel_type)
    rel.setAttribute("Target", target)
    if external:
        rel.setAttribute("TargetMode", "External")
    rels_dom.documentElement.appendChild(rel)
    return rel.getAttribute("Id")


def add_rel(rels_dom, url: str) -> str:
    """A fresh hyperlink relationship to `url`."""
    return add_relationship(rels_dom, HYPERLINK_TYPE, url, external=True)


def prune_orphan_rels(slide_dom, rels_dom) -> None:
    """Remove the hyperlink and picture relationships nothing on the slide uses any more.

    Repointing a run or a picture mints a fresh relationship and leaves the previous one behind; a
    removed icon leaves its picture's. A left-over picture relationship would also keep its media file
    in the deck (lib/package.py sweeps only what no relationship reaches). Only these two types are
    touched, and only when no element references their id: the layout, notes … stay."""
    used = {el.getAttribute(attr) for el in slide_dom.getElementsByTagName("*")
            for attr in ("r:id", "r:embed", "r:link")}
    for rel in list(rels_dom.getElementsByTagName("Relationship")):
        if rel.getAttribute("Type") in (HYPERLINK_TYPE, IMAGE_TYPE) and rel.getAttribute("Id") not in used:
            rel.parentNode.removeChild(rel)


# --------------------------------------------------------------------------- shapes, pictures -----
def fresh_ids(node, dom) -> None:
    """Give a new shape (and everything inside it) shape ids no other shape on the slide uses."""
    used = [int(c.getAttribute("id")) for c in dom.getElementsByTagName("p:cNvPr")
            if c.getAttribute("id").isdigit()]
    nxt = max(used, default=1) + 1
    for cnv in node.getElementsByTagName("p:cNvPr"):
        cnv.setAttribute("id", str(nxt))
        nxt += 1


def embed_media(root: Path, rels_dom, source: Path, media_name: str) -> str:
    """Store `source` in the deck as ppt/media/<media_name> behind a FRESH picture relationship of the
    slide (never repoint an existing one: slides share them). Returns the relationship id."""
    media = root / "ppt" / "media"
    media.mkdir(parents=True, exist_ok=True)
    shutil.copy(source, media / media_name)
    return add_relationship(rels_dom, IMAGE_TYPE, f"../media/{media_name}")


def new_picture(dom, rels_dom, root: Path, source: Path, media_name: str, name: str,
                box: tuple[int, int, int, int], descr: str = ""):
    """A new `<p:pic>` showing `source` in `box` (x, y, cx, cy), with an id of its own; the caller
    puts it in the shape tree."""
    pic = minidom.parseString(_PICTURE).documentElement
    for attr in ("xmlns:p", "xmlns:a", "xmlns:r"):
        pic.removeAttribute(attr)   # the slide root already declares these prefixes
    pic = dom.importNode(pic, True)
    cnv = pic.getElementsByTagName("p:cNvPr")[0]
    cnv.setAttribute("name", name)
    if descr:
        cnv.setAttribute("descr", descr)
    pic.getElementsByTagName("a:blip")[0].setAttribute("r:embed", embed_media(root, rels_dom, source, media_name))
    off, ext = pic.getElementsByTagName("a:off")[0], pic.getElementsByTagName("a:ext")[0]
    off.setAttribute("x", str(box[0]))
    off.setAttribute("y", str(box[1]))
    ext.setAttribute("cx", str(box[2]))
    ext.setAttribute("cy", str(box[3]))
    fresh_ids(pic, dom)
    return pic


# --------------------------------------------------------------------------- paragraphs -----------
def paragraphs(sp) -> list:
    """Direct text paragraphs of a shape's txBody (skips paragraphs nested in child shapes)."""
    bodies = sp.getElementsByTagName("p:txBody")
    if not bodies:
        return []
    return [p for p in bodies[0].getElementsByTagName("a:p")]


def para_runs(p) -> list:
    return p.getElementsByTagName("a:r")


def remove_node(node) -> None:
    if node.parentNode is not None:
        node.parentNode.removeChild(node)
