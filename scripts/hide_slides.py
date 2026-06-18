#!/usr/bin/env python3
"""Hide (never delete) slides in an UNPACKED deck directory by setting show="0".

Resolves true presentation order from ppt/presentation.xml -> rels -> slideN.xml, so callers
pass 1-based display positions (matching the deck/slide-map), not raw file numbers.

Usage:
  hide_slides.py UNPACKED_DIR 18 19 20      # hide by display position
  hide_slides.py UNPACKED_DIR --list        # print display order -> file mapping
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from xml.dom import minidom


def slide_order(unpacked: Path) -> list[Path]:
    pres = unpacked / "ppt" / "presentation.xml"
    rels = unpacked / "ppt" / "_rels" / "presentation.xml.rels"
    rid_to_target = {}
    rdom = minidom.parse(str(rels))
    for r in rdom.getElementsByTagName("Relationship"):
        rid_to_target[r.getAttribute("Id")] = r.getAttribute("Target")
    order: list[Path] = []
    pdom = minidom.parse(str(pres))
    lst = pdom.getElementsByTagName("p:sldIdLst")
    if not lst:
        return order
    for sld in lst[0].getElementsByTagName("p:sldId"):
        rid = sld.getAttribute("r:id")
        target = rid_to_target.get(rid, "")
        order.append((unpacked / "ppt" / target.replace("../", "")).resolve())
    return order


def set_hidden(slide_file: Path) -> bool:
    text = slide_file.read_text(encoding="utf-8")
    m = re.search(r"<p:sld\b[^>]*>", text)
    if not m:
        raise RuntimeError(f"no <p:sld> root in {slide_file}")
    tag = m.group(0)
    if re.search(r'\bshow="0"', tag):
        return False  # already hidden
    if re.search(r'\bshow="[^"]*"', tag):
        new_tag = re.sub(r'\bshow="[^"]*"', 'show="0"', tag)
    else:
        new_tag = tag[:-1] + ' show="0">'
    slide_file.write_text(text[: m.start()] + new_tag + text[m.end():], encoding="utf-8")
    return True


def main() -> int:
    if len(sys.argv) < 3:
        raise SystemExit(__doc__)
    unpacked = Path(sys.argv[1]).resolve()
    order = slide_order(unpacked)
    if sys.argv[2] == "--list":
        for i, f in enumerate(order, 1):
            print(f"{i:>3}  {f.name}")
        return 0
    positions = [int(a) for a in sys.argv[2:]]
    for pos in positions:
        if not 1 <= pos <= len(order):
            raise SystemExit(f"position {pos} out of range (1..{len(order)})")
        changed = set_hidden(order[pos - 1])
        print(f"slide {pos} ({order[pos - 1].name}): {'hidden' if changed else 'already hidden'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
