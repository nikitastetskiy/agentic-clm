"""The deck as a package (lib/package.py, lib/deck.py): what pack refuses to write, what clean
removes, how a slide is copied, and that the same parts always give the same file."""
import re
import tempfile
import unittest
import zipfile
from pathlib import Path

import helpers

from lib import deck, package

TEMPLATE = helpers.REPO / "templates" / "cadence-template.pptx"


class Package(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.out = self.tmp / "deck.pptx"
        self.unpacked = self.tmp / "unpacked"
        package.unpack(TEMPLATE, self.unpacked)

    def slide(self, role: str) -> Path:
        return self.unpacked / helpers.slide_member(TEMPLATE, role)

    def pack(self) -> list[str]:
        return package.pack(self.unpacked, self.out, original=TEMPLATE)

    def test_the_template_packs_and_the_same_parts_give_the_same_file(self):
        self.assertEqual(self.pack(), [])
        first = self.out.read_bytes()
        self.assertEqual(self.pack(), [])
        self.assertEqual(self.out.read_bytes(), first)
        with zipfile.ZipFile(self.out) as z:
            self.assertEqual(z.namelist()[0], "[Content_Types].xml")

    def test_a_broken_deck_is_not_written(self):
        slide = self.slide("terraform-enterprise-releases")
        xml = slide.read_text(encoding="utf-8")
        broken = re.sub(r'r:id="rId\d+"', 'r:id="rId999"', xml, count=1)   # a link to no relationship
        self.assertNotEqual(broken, xml)
        slide.write_text(broken, encoding="utf-8")
        problems = self.pack()
        self.assertTrue(any("rId999" in p for p in problems), problems)
        self.assertFalse(self.out.exists())

    def test_each_structural_fault_is_named(self):
        next(iter(sorted((self.unpacked / "ppt" / "media").iterdir()))).unlink()   # a picture a slide uses
        (self.unpacked / "ppt" / "stray.xyz").write_text("x", encoding="utf-8")    # a part of no declared type
        slide = self.slide("vault-blogs")
        slide.write_text(slide.read_text(encoding="utf-8")[:-20], encoding="utf-8")  # XML cut short
        problems = "\n".join(self.pack())
        for fault in ("which is not in the package", "ppt/stray.xyz: no content type", "not well-formed XML"):
            self.assertIn(fault, problems)
        self.assertFalse(self.out.exists())

    def test_a_fault_the_original_already_has_is_not_held_against_the_edit(self):
        (self.unpacked / "ppt" / "stray.xyz").write_text("x", encoding="utf-8")
        flawed = self.tmp / "flawed-original.pptx"
        with zipfile.ZipFile(TEMPLATE) as src, zipfile.ZipFile(flawed, "w") as dst:
            for item in src.infolist():
                dst.writestr(item, src.read(item.filename))
            dst.writestr("ppt/stray.xyz", "x")
        self.assertEqual(package.pack(self.unpacked, self.out, original=flawed), [])
        self.assertTrue(self.out.exists())

    def test_a_pack_must_name_an_original_that_exists(self):
        with self.assertRaises(TypeError):
            package.pack(self.unpacked, self.out)
        with self.assertRaises(FileNotFoundError):
            package.pack(self.unpacked, self.out, original=self.tmp / "missing.pptx")
        self.assertFalse(self.out.exists())

    def test_a_slide_that_is_not_shown_goes_with_what_only_it_used(self):
        featured = self.slide("featured-event")
        deck.set_slide_order(self.unpacked, [s for s in deck.ordered_slides(self.unpacked) if s != featured])
        removed = deck.clean(self.unpacked)
        self.assertIn(f"ppt/slides/{featured.name}", removed)
        self.assertIn(f"ppt/slides/_rels/{featured.name}.rels", removed)
        self.assertTrue(any(name.startswith("ppt/media/") for name in removed), removed)
        self.assertFalse(featured.exists())
        self.assertEqual(self.pack(), [])            # no dangling relationship or content type is left
        self.assertEqual(deck.clean(self.unpacked), [])

    def test_copies_of_a_slide_are_shown_one_after_the_other_each_with_its_own_id(self):
        slides = deck.ordered_slides(self.unpacked)
        featured = self.slide("featured-event")
        first = deck.duplicate_after(self.unpacked, featured, slides[0])
        second = deck.duplicate_after(self.unpacked, featured, first)       # as for three featured events
        self.assertEqual(deck.ordered_slides(self.unpacked)[:3], [slides[0], first, second])
        self.assertEqual(second.read_bytes(), featured.read_bytes())
        ids = re.findall(r'<p:sldId id="(\d+)" r:id="rId\d+"/>', (self.unpacked / "ppt" / "presentation.xml").read_text())
        self.assertEqual((len(ids), len(set(ids))), (len(slides) + 2, len(slides) + 2))
        self.assertEqual(self.pack(), [])


if __name__ == "__main__":
    unittest.main()
