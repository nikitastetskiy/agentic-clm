"""A .pptx as a package: a zip of parts, tied together by relationships and a table of content types.

The builder, the template cutter and the gate all work on an unpacked deck. This module unpacks one,
packs one again — refusing a package whose structure is broken — and sweeps out the parts nothing
reaches any more. It knows nothing about slides; lib/deck.py does.
"""
from __future__ import annotations

import os
import posixpath
import zipfile
from pathlib import Path
from xml.dom import minidom

CONTENT_TYPES = "[Content_Types].xml"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
ZIP_DATE = (1980, 1, 1, 0, 0, 0)   # every member's timestamp: the same parts give the same file


def unpack(deck: Path, dest: Path) -> None:
    """Extract a deck's parts into `dest`, each exactly as it is stored."""
    with zipfile.ZipFile(deck) as z:
        z.extractall(dest)


def rels_name(part: str) -> str:
    """The relationships part of `part` (package paths, "/"-separated): ppt/slides/_rels/slide1.xml.rels.
    The package's own ("") is _rels/.rels."""
    return posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")


def targets(rels_doc, owner: str) -> list[tuple[str, str]]:
    """(relationship id, the part it points at) for every relationship of `owner` that stays inside
    the package. A Target is relative to the part that owns the relationship."""
    out = []
    for rel in rels_doc.getElementsByTagName("Relationship"):
        if rel.getAttribute("TargetMode") != "External":
            target = rel.getAttribute("Target")
            part = target[1:] if target.startswith("/") else posixpath.normpath(
                posixpath.join(posixpath.dirname(owner), target))
            out.append((rel.getAttribute("Id"), part))
    return out


# --------------------------------------------------------------------------- the structural check -
def problems(names: set[str], read) -> list[str]:
    """What is structurally wrong with a package (`names`: its part paths, `read(name)` -> bytes):

      * an XML part that does not parse;
      * a part with no content type, or a content-type override for a part that is not there;
      * a relationship id used in a part (r:id, r:embed, r:link …) that its .rels does not define, a
        relationship pointing at a part that is not there, or two relationships with one id.

    These are the faults that make PowerPoint refuse a file or offer to repair it and that an edit of
    slide XML can introduce. It is not a schema check: an element in the wrong place passes."""
    docs, out = {}, []
    for name in sorted(names):
        if name.endswith((".xml", ".rels")):
            try:
                docs[name] = minidom.parseString(read(name))
            except Exception as exc:  # noqa: BLE001 — expat's errors and a non-XML payload alike
                out.append(f"{name}: not well-formed XML ({exc})")
    if CONTENT_TYPES not in docs:
        return out + [f"{CONTENT_TYPES}: missing or unreadable"]
    return out + _type_problems(docs[CONTENT_TYPES], names) + _relationship_problems(docs, names)


def _type_problems(types, names: set[str]) -> list[str]:
    defaults = {d.getAttribute("Extension").lower() for d in types.getElementsByTagName("Default")}
    overrides = {o.getAttribute("PartName").lstrip("/") for o in types.getElementsByTagName("Override")}
    return ([f"{CONTENT_TYPES}: override for a part that is not in the package: {n}"
             for n in sorted(overrides - names)]
            + [f"{n}: no content type" for n in sorted(names - overrides - {CONTENT_TYPES})
               if posixpath.splitext(n)[1][1:].lower() not in defaults])


def _relationship_problems(docs: dict, names: set[str]) -> list[str]:
    out = []
    for name, doc in docs.items():
        if name.endswith(".rels"):
            folder = posixpath.dirname(posixpath.dirname(name))
            owner = posixpath.join(folder, posixpath.basename(name)[:-len(".rels")])
            ids = [r.getAttribute("Id") for r in doc.getElementsByTagName("Relationship")]
            out += [f"{name}: relationship id {rid} is defined twice"
                    for rid in sorted({i for i in ids if ids.count(i) > 1})]
            out += [f"{name}: {rid} points at {part}, which is not in the package"
                    for rid, part in targets(doc, owner) if part not in names]
            continue
        rels = docs.get(rels_name(name))
        defined = {r.getAttribute("Id") for r in rels.getElementsByTagName("Relationship")} if rels else set()
        used = {attr.value for el in doc.getElementsByTagName("*")
                for attr in (el.attributes.item(i) for i in range(el.attributes.length))
                if attr.namespaceURI == REL_NS and attr.value}
        out += [f"{name}: uses relationship {rid}, which {rels_name(name)} does not define"
                for rid in sorted(used - defined)]
    return out


# --------------------------------------------------------------------------- pack, sweep ----------
def _files(unpacked: Path) -> dict[str, Path]:
    return {p.relative_to(unpacked).as_posix(): p for p in sorted(Path(unpacked).rglob("*")) if p.is_file()}


def pack(src: Path, deck: Path, *, original: Path) -> list[str]:
    """Zip the unpacked deck `src` as `deck` — unless its structure is broken. Returns the problems
    found (see `problems`); the file is written only when there are none.

    `original` is what the edit started from (the template, or the reference deck a template is cut
    from): a fault it already has is not the edit's and is not held against the result. Members are
    written in a fixed order with a fixed timestamp, so the same parts give the same file."""
    if not Path(original).is_file():
        raise FileNotFoundError(f"no original deck to validate the pack against: {original}")
    files = _files(src)
    found = problems(set(files), lambda name: files[name].read_bytes())
    if found:
        with zipfile.ZipFile(original) as z:
            known = set(problems({n for n in z.namelist() if not n.endswith("/")}, z.read))
        found = [p for p in found if p not in known]
    if found:
        return found
    tmp = Path(deck).with_name(Path(deck).name + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for name in sorted(files, key=lambda n: (n != CONTENT_TYPES, n)):   # the content types lead
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, files[name].read_bytes())
    os.replace(tmp, deck)
    return []


def sweep(unpacked: Path) -> list[str]:
    """Delete every part that no chain of relationships reaches from the package itself, and the
    content-type overrides of parts that are gone. So a part is dropped by dropping the relationship
    that points at it. Returns the deleted parts' package paths."""
    reached, queue = {CONTENT_TYPES}, [""]          # "" is the package: its relationships are _rels/.rels
    while queue:
        part = queue.pop()
        rels = unpacked / rels_name(part)
        if not rels.is_file():
            continue
        reached.add(rels_name(part))
        for _rid, target in targets(minidom.parse(str(rels)), part):
            if target not in reached and (unpacked / target).is_file():
                reached.add(target)
                queue.append(target)
    removed = [name for name in _files(unpacked) if name not in reached]
    for name in removed:
        (unpacked / name).unlink()

    types_path = unpacked / CONTENT_TYPES
    types = minidom.parse(str(types_path))
    for override in list(types.getElementsByTagName("Override")):
        if not (unpacked / override.getAttribute("PartName").lstrip("/")).exists():
            override.parentNode.removeChild(override)
    types_path.write_text(types.toxml(), encoding="utf-8")
    return removed
