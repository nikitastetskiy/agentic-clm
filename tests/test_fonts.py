"""Embedded fonts: a deck must not embed one font under another font's name (lib/fonts.py)."""
import shutil
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

import helpers

from lib import fonts

TEMPLATE = helpers.REPO / "templates" / "cadence-template.pptx"


def eot(family: str) -> bytes:
    """The smallest EOT header that names `family` (enough for lib/fonts.py to read)."""
    head = bytearray(84)
    struct.pack_into("<H", head, 34, fonts.EOT_MAGIC)
    name = family.encode("utf-16-le")
    struct.pack_into("<H", head, 82, len(name))
    return bytes(head) + name


def package(root: Path, entries: dict[str, dict[str, str]]) -> None:
    """A minimal unpacked deck embedding `entries`: {typeface: {slot: family of the data}}."""
    (root / "ppt" / "_rels").mkdir(parents=True)
    (root / "ppt" / "fonts").mkdir()
    fonts_xml, rels_xml, n = [], [], 0
    for typeface, slots in entries.items():
        children = []
        for slot, family in slots.items():
            n += 1
            (root / "ppt" / "fonts" / f"font{n}.fntdata").write_bytes(eot(family))
            rels_xml.append(f'<Relationship Id="rId{n}" Type="font" Target="fonts/font{n}.fntdata"/>')
            children.append(f'<p:{slot} r:id="rId{n}"/>')
        fonts_xml.append(f'<p:embeddedFont><p:font typeface="{typeface}"/>{"".join(children)}</p:embeddedFont>')
    (root / "ppt" / "presentation.xml").write_text(
        '<p:presentation xmlns:p="p" xmlns:r="r"><p:embeddedFontLst>' + "".join(fonts_xml)
        + "</p:embeddedFontLst></p:presentation>", encoding="utf-8")
    (root / "ppt" / "_rels" / "presentation.xml.rels").write_text(
        '<Relationships xmlns="rels">' + "".join(rels_xml) + "</Relationships>", encoding="utf-8")


class EmbeddedFonts(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="fonts-"))

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_the_eot_header_names_the_real_family(self):
        self.assertEqual(fonts.eot_family(eot("Inter Light")), "Inter Light")
        self.assertIsNone(fonts.eot_family(b"\x00\x01\x00\x00 a TrueType file, not EOT" + bytes(80)))

    def test_a_font_embedded_under_another_name_is_dropped_and_the_rest_kept(self):
        package(self.root, {"Inter": {"regular": "Inter", "bold": "Inter"},
                            "Inter Light": {"regular": "Calibri", "bold": "Calibri"},
                            "Poppins": {"regular": "Poppins", "italic": "Calibri"}})
        self.assertEqual({(f["typeface"], f["slot"]) for f in fonts.mismatched(self.root)},
                         {("Inter Light", "regular"), ("Inter Light", "bold"), ("Poppins", "italic")})
        self.assertEqual(fonts.drop_mismatched(self.root), ["Inter Light", "Poppins"])
        self.assertEqual(fonts.mismatched(self.root), [])
        self.assertEqual([(f["typeface"], f["slot"]) for f in fonts.embedded(self.root)],
                         [("Inter", "regular"), ("Inter", "bold"), ("Poppins", "regular")])
        self.assertEqual(sorted(p.name for p in (self.root / "ppt" / "fonts").iterdir()),
                         ["font1.fntdata", "font2.fntdata", "font5.fntdata"])
        rels = (self.root / "ppt" / "_rels" / "presentation.xml.rels").read_text(encoding="utf-8")
        self.assertNotIn("font3.fntdata", rels)

    def test_the_template_embeds_each_font_under_its_own_name(self):
        with zipfile.ZipFile(TEMPLATE) as z:
            z.extractall(self.root)
        self.assertEqual(fonts.mismatched(self.root), [])
        self.assertEqual({f["typeface"] for f in fonts.embedded(self.root)},
                         {"Inter", "Inter Medium", "Inter SemiBold", "Poppins"})
