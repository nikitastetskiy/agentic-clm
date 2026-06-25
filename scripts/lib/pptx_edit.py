"""Shared OOXML editing helpers for the deterministic slide editors (blogs, events, releases).

These wrap the gritty, error-prone parts of editing a PowerPoint slide's XML by hand — unpack/pack,
relationship (hyperlink) handling, and run/paragraph text edits — the CORRECT way, so each editor
stays small and consistent. The golden rules encoded here (learned the hard way, see corrections):

  * NEVER mutate an existing relationship's Target — rIds are frequently SHARED across entries, so
    repointing one corrupts another. Always mint a FRESH rel with `add_rel()` and repoint the run.
  * Edit text INSIDE existing `<a:t>` runs (preserving every `<a:rPr>`); to shorten content, blank
    or remove trailing runs/paragraphs rather than rebuilding a shape from scratch.
"""
from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import sys
from pathlib import Path
from xml.dom import minidom

REPO_ROOT = Path(__file__).resolve().parents[2]
UNPACK = REPO_ROOT / ".github/skills/pptx/scripts/office/unpack.py"
PACK = REPO_ROOT / ".github/skills/pptx/scripts/office/pack.py"
ADD_SLIDE = REPO_ROOT / ".github/skills/pptx/scripts/add_slide.py"
EMU_PER_IN = 914400
HYPERLINK_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"
IMAGE_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"


# --------------------------------------------------------------------------- pack/unpack ----------
def unpack(deck: Path, dest: Path) -> None:
    subprocess.run([sys.executable, str(UNPACK), str(deck), str(dest)], check=True, capture_output=True)


def pack(src: Path, deck: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(PACK), str(src), str(deck), "--validate", "true"],
                          capture_output=True, text=True)


def _load_add_slide():
    spec = importlib.util.spec_from_file_location("pptx_add_slide", ADD_SLIDE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def duplicate_slide_after(unpacked: Path, src_n: int, anchor_n: int) -> int | None:
    """Duplicate ppt/slides/slide{src_n}.xml as a NEW slide and place it RIGHT AFTER slide{anchor_n}
    in the deck's display order. Reuses the vendored pptx skill's add_slide.py for the file/rels/
    content-type/presentation-rels registration, then splices the <p:sldId> into presentation.xml.
    Returns the new slide's file number, or None on failure (caller treats failure as non-fatal)."""
    try:
        add = _load_add_slide()
        slides = unpacked / "ppt" / "slides"
        src = f"slide{src_n}.xml"
        if not (slides / src).exists():
            return None
        new_n = add.get_next_slide_number(slides)
        dest = f"slide{new_n}.xml"
        shutil.copy2(slides / src, slides / dest)
        src_rels = slides / "_rels" / f"{src}.rels"
        if src_rels.exists():
            shutil.copy2(src_rels, slides / "_rels" / f"{dest}.rels")
        add._add_to_content_types(unpacked, dest)
        rid = add._add_to_presentation_rels(unpacked, dest)
        if not _insert_sldid_after(unpacked, anchor_n, rid):
            return None
        return new_n
    except Exception:  # noqa: BLE001 — slide growth is best-effort; never crash the editor
        return None


def _insert_sldid_after(unpacked: Path, anchor_n: int, new_rid: str) -> bool:
    """Splice a <p:sldId> for `new_rid` into presentation.xml immediately after slide{anchor_n}'s."""
    prels = (unpacked / "ppt" / "_rels" / "presentation.xml.rels").read_text(encoding="utf-8")
    m = (re.search(rf'Id="(rId\d+)"[^>]*Target="slides/slide{anchor_n}\.xml"', prels)
         or re.search(rf'Target="slides/slide{anchor_n}\.xml"[^>]*Id="(rId\d+)"', prels))
    if not m:
        return False
    anchor_rid = m.group(1)
    pres_path = unpacked / "ppt" / "presentation.xml"
    txt = pres_path.read_text(encoding="utf-8")
    ids = [int(x) for x in re.findall(r'<p:sldId[^>]*id="(\d+)"', txt)]
    nid = (max(ids) + 1) if ids else 256
    am = re.search(rf'<p:sldId[^>]*r:id="{anchor_rid}"\s*/>', txt)
    if not am:
        return False
    new_el = f'<p:sldId id="{nid}" r:id="{new_rid}"/>'
    pres_path.write_text(txt[:am.end()] + new_el + txt[am.end():], encoding="utf-8")
    return True


# --------------------------------------------------------------------------- geometry/text --------
def emu_to_in(v: str | None) -> float | None:
    try:
        return round(int(v) / EMU_PER_IN, 2)
    except (TypeError, ValueError):
        return None


def shape_xy(sp) -> tuple[float | None, float | None]:
    off = sp.getElementsByTagName("a:off")
    if not off:
        return None, None
    return emu_to_in(off[0].getAttribute("x")), emu_to_in(off[0].getAttribute("y"))


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


# --------------------------------------------------------------------------- relationships --------
def load_rels(rels_file: Path) -> tuple[minidom.Document, dict]:
    dom = minidom.parse(str(rels_file))
    rid_target = {r.getAttribute("Id"): r.getAttribute("Target")
                  for r in dom.getElementsByTagName("Relationship")}
    return dom, rid_target


def add_rel(rels_dom, target: str) -> str:
    """Append a NEW external-hyperlink relationship and return its rId (never mutate a shared one)."""
    container = rels_dom.getElementsByTagName("Relationships")[0]
    nums = [int(r.getAttribute("Id")[3:]) for r in rels_dom.getElementsByTagName("Relationship")
            if r.getAttribute("Id").startswith("rId") and r.getAttribute("Id")[3:].isdigit()]
    new_id = f"rId{(max(nums) + 1) if nums else 1}"
    rel = rels_dom.createElement("Relationship")
    rel.setAttribute("Id", new_id)
    rel.setAttribute("Type", HYPERLINK_TYPE)
    rel.setAttribute("Target", target)
    rel.setAttribute("TargetMode", "External")
    container.appendChild(rel)
    return new_id


def add_image_rel(rels_dom, target: str) -> str:
    """Append a NEW internal image relationship (Type=image, no TargetMode) and return its rId.

    Used to repoint a picture's <a:blip r:embed> at a freshly added media file without mutating any
    existing (possibly shared) image rel — the same shared-rId safety rule as hyperlinks.
    """
    container = rels_dom.getElementsByTagName("Relationships")[0]
    nums = [int(r.getAttribute("Id")[3:]) for r in rels_dom.getElementsByTagName("Relationship")
            if r.getAttribute("Id").startswith("rId") and r.getAttribute("Id")[3:].isdigit()]
    new_id = f"rId{(max(nums) + 1) if nums else 1}"
    rel = rels_dom.createElement("Relationship")
    rel.setAttribute("Id", new_id)
    rel.setAttribute("Type", IMAGE_TYPE)
    rel.setAttribute("Target", target)
    container.appendChild(rel)
    return new_id


def relink_run(run, rels_dom, target: str) -> bool:
    """Point a run's hyperlink at `target` via a FRESH rel (so shared rIds elsewhere are unaffected).

    Returns False if the run has no <a:hlinkClick> (nothing to repoint).
    """
    hl = run_hyperlink(run)
    if hl is None:
        return False
    hl.setAttribute("r:id", add_rel(rels_dom, target))
    return True


def prune_orphan_hyperlinks(slide_dom, rels_dom) -> list[str]:
    """Remove External-hyperlink rels no longer referenced by any run in the slide.

    Repointing a run via `add_rel`/`relink_run` mints a fresh rId and leaves the run's PREVIOUS
    hyperlink rel dangling in the .rels (e.g. a refreshed release entry left rId11 -> an old
    prerelease tag on slide 7). Such orphans are harmless but untidy; this sweeps them after edits.

    SAFE BY CONSTRUCTION: only relationships of the hyperlink Type are ever removed, and only when no
    element in the slide references their Id — so implicit/internal rels (slideLayout, image/media,
    notesSlide, …) are never touched. Returns the list of pruned rIds.
    """
    referenced: set[str] = set()
    for el in slide_dom.getElementsByTagName("*"):
        for attr in ("r:id", "r:embed", "r:link"):
            val = el.getAttribute(attr)
            if val:
                referenced.add(val)
    pruned: list[str] = []
    for rel in list(rels_dom.getElementsByTagName("Relationship")):
        if rel.getAttribute("Type") == HYPERLINK_TYPE and rel.getAttribute("Id") not in referenced:
            rel.parentNode.removeChild(rel)
            pruned.append(rel.getAttribute("Id"))
    return pruned


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
