"""The fonts a deck embeds for machines that do not have its typefaces installed.

A .pptx can carry fonts (ppt/fonts/*.fntdata, listed in presentation.xml's embeddedFontLst), and
PowerPoint draws a typeface with its embedded data whenever that typeface is not installed. Embedded
data that is a different font than its name says therefore breaks the text: the reference deck
embeds Calibri under the names "Inter Light" and "Inter Black", and PowerPoint draws "From Terraform
Releases" with its letters run into each other. Without the entry, PowerPoint falls back to an
installed font and the text stays readable.

The embedded data is Embedded OpenType (EOT); its header names the family it really is.
"""
from __future__ import annotations

import struct
from pathlib import Path
from xml.dom import minidom

EOT_MAGIC = 0x504C
SLOTS = ("p:regular", "p:bold", "p:italic", "p:boldItalic")


def eot_family(data: bytes) -> str | None:
    """The family name in an EOT header, or None when `data` is not EOT."""
    # EOTSize, FontDataSize, Version, Flags, PANOSE[10], Charset, Italic, Weight, fsType, MagicNumber
    # (offset 34), UnicodeRange[16], CodePageRange[8], CheckSumAdjustment, Reserved[16], Padding1,
    # FamilyNameSize (offset 82), FamilyName (UTF-16LE).
    if len(data) < 84 or struct.unpack_from("<H", data, 34)[0] != EOT_MAGIC:
        return None
    size = struct.unpack_from("<H", data, 82)[0]
    return data[84:84 + size].decode("utf-16-le", "replace")


def _package(unpacked: Path):
    pres_path = unpacked / "ppt" / "presentation.xml"
    rels_path = unpacked / "ppt" / "_rels" / "presentation.xml.rels"
    pres, rels = minidom.parse(str(pres_path)), minidom.parse(str(rels_path))
    targets = {r.getAttribute("Id"): r.getAttribute("Target") for r in rels.getElementsByTagName("Relationship")}
    return pres_path, pres, rels_path, rels, targets


def embedded(unpacked: Path) -> list[dict]:
    """One entry per embedded font slot: typeface, slot (regular/bold/…), part and the family its data
    really is (None when the part is missing or not EOT)."""
    _pp, pres, _rp, _rels, targets = _package(unpacked)
    out = []
    for entry in pres.getElementsByTagName("p:embeddedFont"):
        typeface = entry.getElementsByTagName("p:font")[0].getAttribute("typeface")
        for slot in (c for c in entry.childNodes if c.nodeType == c.ELEMENT_NODE and c.tagName in SLOTS):
            part = "ppt/" + targets.get(slot.getAttribute("r:id"), "")
            path = unpacked / part
            family = eot_family(path.read_bytes()) if path.is_file() else None
            out.append({"typeface": typeface, "slot": slot.tagName[2:], "part": part, "family": family})
    return out


def mismatched(unpacked: Path) -> list[dict]:
    """Embedded slots whose data is not the typeface they are listed under."""
    return [f for f in embedded(unpacked) if (f["family"] or "").casefold() != f["typeface"].casefold()]


def drop_mismatched(unpacked: Path) -> list[str]:
    """Remove every embedded slot whose data is another font (and the typeface's entry once it has no
    slot left), with the font parts nothing else uses. Returns the typefaces affected."""
    pres_path, pres, rels_path, rels, targets = _package(unpacked)
    bad = {(f["typeface"], f["slot"]) for f in mismatched(unpacked)}
    dropped_rids, affected = set(), []
    for entry in list(pres.getElementsByTagName("p:embeddedFont")):
        typeface = entry.getElementsByTagName("p:font")[0].getAttribute("typeface")
        for slot in [c for c in entry.childNodes if c.nodeType == c.ELEMENT_NODE and c.tagName in SLOTS]:
            if (typeface, slot.tagName[2:]) in bad:
                dropped_rids.add(slot.getAttribute("r:id"))
                entry.removeChild(slot)
                if typeface not in affected:
                    affected.append(typeface)
        if not any(c.nodeType == c.ELEMENT_NODE and c.tagName in SLOTS for c in entry.childNodes):
            entry.parentNode.removeChild(entry)
    kept_targets = {targets[r] for r in targets if r not in dropped_rids}
    for rel in list(rels.getElementsByTagName("Relationship")):
        rid = rel.getAttribute("Id")
        if rid in dropped_rids:
            rel.parentNode.removeChild(rel)
            if targets[rid] not in kept_targets:
                (unpacked / "ppt" / targets[rid]).unlink(missing_ok=True)
    for lst in pres.getElementsByTagName("p:embeddedFontLst"):
        if not lst.getElementsByTagName("p:embeddedFont"):
            lst.parentNode.removeChild(lst)
    pres_path.write_text(pres.toxml(), encoding="utf-8")
    rels_path.write_text(rels.toxml(), encoding="utf-8")
    return affected
