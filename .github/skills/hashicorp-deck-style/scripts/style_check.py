#!/usr/bin/env python3
"""Style-preservation guard: flag foreign colors/fonts introduced into edited slides.

Scans the given slides (by 1-based display position) in an UNPACKED deck and reports any
srgbClr hex or typeface not present in palette.json. Theme references (scheme colors, +mn-/+mj-)
are always allowed.

Usage: style_check.py UNPACKED_DIR 7 8 9 ...
Exit 0 = clean, 1 = foreign values found.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from xml.dom import minidom

SKILL_DIR = Path(__file__).resolve().parents[1]
PALETTE = json.loads((SKILL_DIR / "palette.json").read_text(encoding="utf-8"))
ALLOWED_HEX = {h.upper() for h in PALETTE["allowed_hex"]}
ALLOWED_FONTS = {f.lower() for group in PALETTE["fonts"].values() for f in group}


def slide_order(unpacked: Path) -> list[Path]:
    rels = unpacked / "ppt" / "_rels" / "presentation.xml.rels"
    rid = {r.getAttribute("Id"): r.getAttribute("Target")
           for r in minidom.parse(str(rels)).getElementsByTagName("Relationship")}
    pres = minidom.parse(str(unpacked / "ppt" / "presentation.xml"))
    out = []
    lst = pres.getElementsByTagName("p:sldIdLst")
    if lst:
        for s in lst[0].getElementsByTagName("p:sldId"):
            out.append((unpacked / "ppt" / rid.get(s.getAttribute("r:id"), "").replace("../", "")).resolve())
    return out


def main() -> int:
    unpacked = Path(sys.argv[1]).resolve()
    positions = [int(a) for a in sys.argv[2:]]
    order = slide_order(unpacked)
    foreign = False
    for pos in positions:
        if not 1 <= pos <= len(order):
            print(f"slide {pos}: out of range")
            continue
        text = order[pos - 1].read_text(encoding="utf-8")
        hexes = {h.upper() for h in re.findall(r'srgbClr val="([0-9A-Fa-f]{6})"', text)}
        fonts = {f for f in re.findall(r'typeface="([^"]+)"', text)}
        bad_hex = sorted(hexes - ALLOWED_HEX)
        bad_fonts = sorted(f for f in fonts if not f.startswith("+") and f.lower() not in ALLOWED_FONTS)
        if bad_hex or bad_fonts:
            foreign = True
            print(f"slide {pos} ({order[pos-1].name}): foreign hex={bad_hex} fonts={bad_fonts}")
        else:
            print(f"slide {pos} ({order[pos-1].name}): OK")
    return 1 if foreign else 0


if __name__ == "__main__":
    raise SystemExit(main())
