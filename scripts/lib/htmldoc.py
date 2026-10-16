"""Small helpers for reading HashiCorp docs pages (developer.hashicorp.com) as structured text.

Those pages are server-rendered: every release / changelog entry is a heading with a stable ``id``
(``vault-2-1-2``, ``2-0-8``, ``2026-10-02``) followed by sibling headings, paragraphs and lists. The
fetchers walk that sibling chain instead of scraping the page's flattened text, so a version number that
merely appears in a table or sentence is never mistaken for an entry.
"""
from __future__ import annotations

import re

from lxml import html as lxml_html

HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
# The inline "Enterprise" badge inside a list item is rendered as text — a label, not prose.
_ENTERPRISE_BADGE = './/*[contains(@class, "enterprise-alert")]'
_WS = re.compile(r"\s+")


def parse(html_text: str):
    return lxml_html.fromstring(html_text)


def text_of(node) -> str:
    return _WS.sub(" ", node.text_content()).strip()


def headings_with_id(doc, pattern: str, tags: tuple[str, ...] = HEADINGS) -> list:
    """Headings (document order) whose id fully matches the regex `pattern`."""
    rx = re.compile(pattern)
    return [el for el in doc.iter(*tags) if rx.fullmatch(el.get("id") or "")]


def entry_body(heading, is_entry) -> list:
    """Sibling elements after an entry `heading` up to (not including) the next entry heading, as
    decided by ``is_entry(element)``. Entries are delimited by their id pattern rather than by heading
    level because the docs are not consistent about levels (older Terraform Enterprise releases sit
    one level below newer ones on the same page)."""
    out = []
    for sib in heading.itersiblings():
        if not isinstance(sib.tag, str):
            continue
        if sib.tag in HEADINGS and is_entry(sib):
            break
        out.append(sib)
    return out


def _drop_enterprise_badges(node) -> bool:
    """Remove inline "Enterprise" badges under `node`, keeping the text that follows them."""
    found = False
    for badge in node.xpath(_ENTERPRISE_BADGE):
        if badge.getparent() is not None:
            badge.drop_tree()
            found = True
    return found


def list_items(node) -> list[dict]:
    """A <ul>/<ol>'s direct items as ``{"lead", "text", "enterprise"}``.

    ``lead`` is the item's bold lead-in (``<strong>Secrets sync</strong>: Fixed …`` -> "Secrets sync"),
    ``text`` the remaining sentence(s). The "Enterprise" badge is dropped from the text and reported
    as a flag."""
    items = []
    for li in node.xpath("./li"):
        enterprise = _drop_enterprise_badges(li)
        lead = ""
        strong = li.xpath("./strong | ./p/strong")
        full = text_of(li)
        if strong and full.startswith(text_of(strong[0])):
            lead = text_of(strong[0]).rstrip(":").strip()
            full = full[len(text_of(strong[0])):].lstrip(" :–—-").strip()
        if full and full.lower().rstrip(".") != "none":
            items.append({"lead": lead, "text": full, "enterprise": enterprise})
    return items


def sections(nodes: list) -> dict[str, list[dict]]:
    """Group an entry's body into ``{section heading: [list items]}``. Every heading in the run opens
    a section; paragraphs outside a list become lead-less items; content before the first heading is
    filed under ""."""
    out: dict[str, list[dict]] = {}
    current = ""
    for node in nodes:
        if node.tag in HEADINGS:
            _drop_enterprise_badges(node)
            current = text_of(node)
            out.setdefault(current, [])
        elif node.tag in ("ul", "ol"):
            out.setdefault(current, []).extend(list_items(node))
        elif node.tag == "p":
            txt = text_of(node)
            if txt:
                out.setdefault(current, []).append({"lead": "", "text": txt, "enterprise": False})
    return {k: v for k, v in out.items() if v}
