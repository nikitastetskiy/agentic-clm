"""Deck-level operations on an UNPACKED .pptx: slide order, role stamps, keeping / duplicating slides.

The weekly build addresses slides by ROLE, never by number. A role is a short stamp stored in the
slide's own XML (``<p:cSld name="auto-pptx:<role>">`` — PowerPoint preserves it and never shows it),
written once by scripts/make_template.py when the template is cut from a reference deck. Looking slides
up by that stamp means a reordered or extended reference deck cannot silently retarget an automation
(the failure mode of the old slide-number map).
"""
from __future__ import annotations

import importlib.util
import re
from datetime import datetime, timezone
from pathlib import Path
from xml.dom import minidom

from lib import pptx_edit as px

ROLE_PREFIX = "auto-pptx:"
CLEAN = px.REPO_ROOT / ".github/skills/pptx/scripts/clean.py"
SLIDE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide"
SECTION_EXT_URI = "{521415D9-36F7-43E2-AB2F-B90AF26B5E84}"
_ROLE_RE = re.compile(r'<p:cSld\b[^>]*\bname="' + re.escape(ROLE_PREFIX) + r'([^"]+)"')
# Relationship types a template slide must not carry: speaker notes and review comments are written
# for an internal audience and would otherwise ride along into every weekly deck.
PRIVATE_REL_SUFFIXES = ("/notesSlide", "/comments")


# --------------------------------------------------------------------------- slide order ----------
def _pres_rels(unpacked: Path) -> dict[str, str]:
    """rId -> slide file name ('slide7.xml') for every slide relationship of the presentation."""
    rels = minidom.parse(str(unpacked / "ppt" / "_rels" / "presentation.xml.rels"))
    out = {}
    for rel in rels.getElementsByTagName("Relationship"):
        if rel.getAttribute("Type") == SLIDE_REL:
            out[rel.getAttribute("Id")] = rel.getAttribute("Target").rsplit("/", 1)[-1]
    return out


def ordered_slides(unpacked: Path) -> list[Path]:
    """Slide files in DISPLAY order (presentation.xml's sldIdLst), not file-number order."""
    rid_file = _pres_rels(unpacked)
    pres = minidom.parse(str(unpacked / "ppt" / "presentation.xml"))
    lst = pres.getElementsByTagName("p:sldIdLst")
    if not lst:
        return []
    return [unpacked / "ppt" / "slides" / rid_file[s.getAttribute("r:id")]
            for s in lst[0].getElementsByTagName("p:sldId") if s.getAttribute("r:id") in rid_file]


def rels_path(slide: Path) -> Path:
    return slide.parent / "_rels" / f"{slide.name}.rels"


def slide_number(slide: Path) -> int:
    return int(re.search(r"slide(\d+)\.xml$", slide.name).group(1))


def set_slide_order(unpacked: Path, slides: list[Path]) -> None:
    """Make the deck show exactly `slides`, in that order. Slides left out become orphans that
    clean() then deletes together with any media only they used."""
    file_rid = {f: rid for rid, f in _pres_rels(unpacked).items()}
    pres_path = unpacked / "ppt" / "presentation.xml"
    pres = minidom.parse(str(pres_path))
    lst = pres.getElementsByTagName("p:sldIdLst")[0]
    by_rid = {s.getAttribute("r:id"): s for s in lst.getElementsByTagName("p:sldId")}
    for node in list(lst.childNodes):
        lst.removeChild(node)
    for slide in slides:
        lst.appendChild(by_rid[file_rid[slide.name]])
    # Sections list slides by id; a list that names removed slides makes PowerPoint offer a repair.
    for ext in list(pres.getElementsByTagName("p:ext")):
        if ext.getAttribute("uri") == SECTION_EXT_URI:
            holder = ext.parentNode
            holder.removeChild(ext)
            if not holder.getElementsByTagName("p:ext"):
                holder.parentNode.removeChild(holder)
    for shows in list(pres.getElementsByTagName("p:custShowLst")):
        shows.parentNode.removeChild(shows)
    pres_path.write_text(pres.toxml(), encoding="utf-8")


def duplicate_after(unpacked: Path, src: Path, anchor: Path) -> Path | None:
    """Copy slide `src` and show the copy right after `anchor`. Returns the new slide file."""
    new_n = px.duplicate_slide_after(unpacked, slide_number(src), slide_number(anchor))
    return None if new_n is None else src.parent / f"slide{new_n}.xml"


def clean(unpacked: Path) -> list[str]:
    """Delete everything no shown slide references (orphan slides, media, notes). Returns the list."""
    spec = importlib.util.spec_from_file_location("pptx_clean", CLEAN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.clean_unused_files(unpacked)


# --------------------------------------------------------------------------- roles ----------------
def get_role(slide: Path) -> str | None:
    m = _ROLE_RE.search(slide.read_text(encoding="utf-8"))
    return m.group(1) if m else None


def set_role(slide: Path, role: str) -> None:
    dom = minidom.parse(str(slide))
    dom.getElementsByTagName("p:cSld")[0].setAttribute("name", ROLE_PREFIX + role)
    slide.write_text(dom.toxml(), encoding="utf-8")


def slides_by_role(unpacked: Path) -> dict[str, list[Path]]:
    """role -> its slide(s) in display order. Unstamped slides are ignored."""
    out: dict[str, list[Path]] = {}
    for slide in ordered_slides(unpacked):
        role = get_role(slide)
        if role:
            out.setdefault(role, []).append(slide)
    return out


def strip_private_rels(slide: Path) -> int:
    """Drop a slide's speaker-notes and comment relationships. Returns how many were removed."""
    rf = rels_path(slide)
    if not rf.exists():
        return 0
    dom = minidom.parse(str(rf))
    removed = 0
    for rel in list(dom.getElementsByTagName("Relationship")):
        if rel.getAttribute("Type").endswith(PRIVATE_REL_SUFFIXES):
            rel.parentNode.removeChild(rel)
            removed += 1
    if removed:
        rf.write_text(dom.toxml(), encoding="utf-8")
    return removed


# --------------------------------------------------------------------------- document properties --
def stamp_properties(unpacked: Path, title: str, when: datetime | None = None) -> None:
    """Rewrite the document properties: the reference deck's author name, slide-title index and
    thumbnail describe the SOURCE deck (including slides that are not in this one)."""
    when = (when or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")
    esc = title.replace("&", "&amp;").replace("<", "&lt;")
    (unpacked / "docProps" / "core.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        f"<dc:title>{esc}</dc:title><dc:creator>auto-pptx</dc:creator>"
        "<cp:lastModifiedBy>auto-pptx</cp:lastModifiedBy><cp:revision>1</cp:revision>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{when}</dcterms:created>'
        f'<dcterms:modified xsi:type="dcterms:W3CDTF">{when}</dcterms:modified></cp:coreProperties>',
        encoding="utf-8")
    (unpacked / "docProps" / "app.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties">'
        f"<Application>auto-pptx</Application><Slides>{len(ordered_slides(unpacked))}</Slides>"
        "</Properties>", encoding="utf-8")
    thumb = unpacked / "docProps" / "thumbnail.jpeg"
    if thumb.exists():
        thumb.unlink()
        root_rels = unpacked / "_rels" / ".rels"
        dom = minidom.parse(str(root_rels))
        for rel in list(dom.getElementsByTagName("Relationship")):
            if rel.getAttribute("Type").endswith("/thumbnail"):
                rel.parentNode.removeChild(rel)
        root_rels.write_text(dom.toxml(), encoding="utf-8")
