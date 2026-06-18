#!/usr/bin/env python3
"""Deterministically extract the editable structure of the automatable slides.

This is the project's "learned geometry": instead of the build agent re-dumping slide XML every
run (slow + burns quota), this script unpacks the deck once and writes data/slide-structure.json
describing, for each slide in config/slide-map.yaml, the editable text shapes — their position,
inferred role (column header/entry, webinar slot, featured field, blog entry), current text, and
per-run hyperlink relationship + target. The agents read this map and edit by role.

Usage:
    python scripts/map_slides.py [path/to/deck.pptx]   # defaults to the single repo-root .pptx

The structure is template-stable (positions don't move between months), so this map stays valid
across runs; regenerating it each run simply keeps it honest if the template is ever revised.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.dom import minidom

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.common import load_config, log, write_output  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
UNPACK = REPO / ".github/skills/pptx/scripts/office/unpack.py"
EMU_PER_IN = 914400

# X-position buckets (inches) for the 3 release columns; mid-points ~0.68 / 3.61 / 6.54.
COL_BOUNDS = [(0.0, 2.0), (2.0, 5.0), (5.0, 12.0)]
# Y threshold separating a column's month header (~1.34) from its release entry (~1.65).
HEADER_Y_MAX = 1.5


def _round(v: str | None, d: int = 2) -> float | None:
    try:
        return round(int(v) / EMU_PER_IN, d)
    except (TypeError, ValueError):
        return None


def _text(node) -> str:
    return "".join(
        t.firstChild.data for t in node.getElementsByTagName("a:t") if t.firstChild
    )


def _rels_targets(rels_path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not rels_path.exists():
        return out
    doc = minidom.parse(str(rels_path))
    for rel in doc.getElementsByTagName("Relationship"):
        out[rel.getAttribute("Id")] = rel.getAttribute("Target")
    return out


def _run_hyperlinks(sp, rels: dict[str, str]) -> list[dict]:
    """Per-run hyperlink info: the run text and its hyperlink target (if any)."""
    links: list[dict] = []
    for r in sp.getElementsByTagName("a:r"):
        rid = None
        for hl in r.getElementsByTagName("a:hlinkClick"):
            rid = hl.getAttribute("r:id") or None
            break
        if rid:
            links.append(
                {
                    "run_text": _text(r).strip(),
                    "rid": rid,
                    "target": rels.get(rid, ""),
                }
            )
    return links


def _shapes(slide_xml: Path, rels: dict[str, str]) -> list[dict]:
    doc = minidom.parse(str(slide_xml))
    shapes: list[dict] = []
    for sp in doc.getElementsByTagName("p:sp"):
        txt = _text(sp).strip()
        if not txt:
            continue
        name = ""
        nv = sp.getElementsByTagName("p:cNvPr")
        if nv:
            name = nv[0].getAttribute("name")
        off = sp.getElementsByTagName("a:off")
        x = _round(off[0].getAttribute("x")) if off else None
        y = _round(off[0].getAttribute("y")) if off else None
        shapes.append(
            {
                "name": name,
                "x": x,
                "y": y,
                "text": txt[:200],
                "hyperlinks": _run_hyperlinks(sp, rels),
            }
        )
    return shapes


def _column_of(x: float | None) -> int | None:
    if x is None:
        return None
    for i, (lo, hi) in enumerate(COL_BOUNDS):
        if lo <= x < hi:
            return i
    return None


def _assign_roles(slide_type: str, shapes: list[dict]) -> None:
    """Annotate each shape with a 'role' string based on the slide type + position."""
    if slide_type == "release_updates":
        for s in shapes:
            col = _column_of(s["x"])
            y = s["y"]
            # Title/subtitle rows sit above the columns (y < ~1.2): not a column.
            if col is None or y is None or y < 1.2:
                s["role"] = "header"
                continue
            kind = "header" if y < HEADER_Y_MAX else "entry"
            # col index 2 == rightmost == latest (anchor)
            anchor = "_latest" if col == 2 else ""
            s["role"] = f"col{col + 1}_{kind}{anchor}"
    elif slide_type == "webinars":
        slots = [s for s in shapes if s["y"] is not None and s["y"] > 1.0]
        slots.sort(key=lambda s: (round(s["y"], 1), s["x"] if s["x"] is not None else 0))
        i = 0
        for s in shapes:
            if s in slots:
                s["role"] = f"slot{i + 1}"
                i += 1
            else:
                s["role"] = "header"
    elif slide_type == "in_person_event":
        # The description is the longest text block; the date/register line contains "Register Here".
        desc = max(shapes, key=lambda s: len(s["text"]), default=None)
        for s in shapes:
            t = s["text"].lower()
            if "top priority" in t:
                s["role"] = "promo_banner"
            elif "register here" in t or "webinar |" in t or t.startswith("event |"):
                s["role"] = "date_register"
            elif s is desc and len(s["text"]) > 60:
                s["role"] = "description"
            else:
                s["role"] = "title"
    elif slide_type == "recent_blogs":
        for s in shapes:
            # Blog entries live in the left list (x ~0.7); the right block is the static taxonomy.
            if s["x"] is not None and s["x"] < 2.0 and s["y"] is not None and s["y"] > 1.0:
                s["role"] = "blog_entry"
            elif s["x"] is not None and s["x"] >= 4.0:
                s["role"] = "static_topics"
            else:
                s["role"] = "header"
    else:
        for s in shapes:
            s["role"] = "other"


def main() -> int:
    cfg = load_config("slide-map.yaml")
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    if src is None:
        decks = sorted(REPO.glob("*.pptx"))
        if not decks:
            raise SystemExit("no .pptx found in repo root; pass a path explicitly")
        src = decks[0]
    log(f"mapping slide structure from: {src.name}")

    tmp = Path(tempfile.mkdtemp(prefix="slidemap-"))
    try:
        subprocess.run(
            [sys.executable, str(UNPACK), str(src), str(tmp)],
            check=True,
            capture_output=True,
        )
        slides_dir = tmp / "ppt" / "slides"

        # Flatten the slide list (some entries use a list of slide numbers).
        entries: list[tuple[int, dict]] = []
        for entry in cfg.get("slides", []):
            ns = entry["n"]
            for n in (ns if isinstance(ns, list) else [ns]):
                entries.append((n, entry))

        structure: dict[str, dict] = {}
        for n, entry in entries:
            sx = slides_dir / f"slide{n}.xml"
            if not sx.exists():
                continue
            rels = _rels_targets(slides_dir / "_rels" / f"slide{n}.xml.rels")
            shapes = _shapes(sx, rels)
            _assign_roles(entry.get("type", ""), shapes)
            structure[str(n)] = {
                "name": entry.get("name"),
                "type": entry.get("type"),
                "product": entry.get("product"),
                "rels_file": f"ppt/slides/_rels/slide{n}.xml.rels",
                "slide_file": f"ppt/slides/slide{n}.xml",
                "editable_shapes": shapes,
            }
            roles = [s["role"] for s in shapes]
            log(f"  slide {n} ({entry.get('type')}): {len(shapes)} shapes -> {roles}")

        write_output("slide-structure.json", {"source_deck": src.name, "slides": structure})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
