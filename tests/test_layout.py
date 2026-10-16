"""The layout rules (lib/layout.py) on small hand-written slides: what the builder does, and what the
gate reports when a slide breaks a rule."""
import unittest
from xml.dom import minidom

import helpers  # noqa: F401  — puts scripts/ on the import path
from lib import layout, slides as sl

PT = 12700
SLIDE = (9144000, 5148263)        # the deck's 720 x 405.4 pt
NS = ('xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
      'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
      'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"')


def xfrm(x, y, cx, cy):
    return f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'


def pic(name, box, *, descr="", prst="rect", crop=""):
    return (f'<p:pic><p:nvPicPr><p:cNvPr id="9" name="{name}" descr="{descr}"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
            f'<p:blipFill><a:blip r:embed="rId9"/>{crop}<a:stretch><a:fillRect/></a:stretch></p:blipFill>'
            f'<p:spPr>{xfrm(*box)}<a:prstGeom prst="{prst}"><a:avLst/></a:prstGeom></p:spPr></p:pic>')


def card(box, date="NOV 4, 2026"):
    return (f'<p:sp><p:nvSpPr><p:cNvPr id="3" name="card"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr>{xfrm(*box)}'
            f'</p:spPr><p:txBody><a:bodyPr/><a:p><a:r><a:t>{date} | WEBINAR | See here</a:t></a:r></a:p>'
            f'<a:p><a:r><a:t>A title</a:t></a:r></a:p></p:txBody></p:sp>')


def slide(*shapes):
    return minidom.parseString(f'<p:sld {NS}><p:cSld><p:spTree>{"".join(shapes)}</p:spTree></p:cSld></p:sld>')


def named(dom, name):
    return next(n for n in sl.top_level(dom, "p:pic", "p:sp") if n.getElementsByTagName("p:cNvPr")[0].getAttribute("name") == name)


class Bounds(unittest.TestCase):
    def test_a_picture_past_the_bottom_is_cropped_not_squashed(self):
        # the cover's background: 405.4 pt tall from 8.35 pt down, on a 405.4 pt slide
        dom = slide(pic("bg", (0, 106045, 7450709, 5148263)))
        self.assertEqual(len(layout.bounds_problems(dom, *SLIDE)), 1)
        self.assertEqual(layout.keep_inside(dom, *SLIDE), ["bg: cropped to the slide"])
        x, y, cx, cy = sl.box(named(dom, "bg"))
        self.assertEqual((x, y, cx), (0, 106045, 7450709))           # width (and so proportions) kept
        self.assertAlmostEqual(y + cy, SLIDE[1], delta=1)
        crop = dom.getElementsByTagName("a:srcRect")[0]
        self.assertAlmostEqual(int(crop.getAttribute("b")) / 100000, 106045 / 5148263, places=4)
        self.assertEqual(layout.bounds_problems(dom, *SLIDE), [])
        blip_fill = dom.getElementsByTagName("p:blipFill")[0]
        self.assertEqual([c.nodeName for c in blip_fill.childNodes], ["a:blip", "a:srcRect", "a:stretch"])

    def test_an_existing_crop_is_extended_not_replaced(self):
        # 10 % already cropped from the top; the box runs 100 pt past the right edge of 720 pt
        dom = slide(pic("p", (8128000, 0, 2286000, 1270000), crop='<a:srcRect t="10000"/>'))
        layout.keep_inside(dom, *SLIDE)
        crop = dom.getElementsByTagName("a:srcRect")[0]
        self.assertEqual(crop.getAttribute("t"), "10000")
        self.assertAlmostEqual(int(crop.getAttribute("r")) / 100000, (8128000 + 2286000 - SLIDE[0]) / 2286000, places=4)

    def test_other_shapes_past_the_edge_are_reported(self):
        dom = slide(card((9000000, 0, 600000, 400000)))
        self.assertEqual(layout.keep_inside(dom, *SLIDE), [])
        self.assertIn("extends past the slide's edges", layout.bounds_problems(dom, *SLIDE)[0])


class Cards(unittest.TestCase):
    def deck(self):
        """Three cards slightly off one line, flags and icons where the reference deck left them."""
        return slide(card((425958, 1397149, 2533396, 1684401)),
                     card((3257296, 1416023, 2529967, 1682750), "DEC 8, 2026"),
                     card((6085205, 1414385, 2533396, 1684401), "DEC 10, 2026"),
                     pic("flag1", (2719890, 1440122, 182880, 182880), prst="ellipse"),
                     pic("flag2", (5525390, 1434965, 182880, 182880), prst="ellipse"),
                     pic("vault2", (3715896, 2654300, 238264, 322485), descr="Vault"),
                     pic("tf2", (3428746, 2697480, 236347, 274320)))

    def test_the_build_puts_everything_on_its_line(self):
        dom = self.deck()
        self.assertTrue(layout.card_problems(dom))
        slots = sl.webinar_slots(dom)
        layout.align_cards(slots)
        layout.place_flag(named(dom, "flag1"), slots[0])
        layout.place_flag(named(dom, "flag2"), slots[1])
        layout.place_icons([named(dom, "tf2"), named(dom, "vault2")], slots[1])
        self.assertEqual(layout.card_problems(dom), [])
        tf, vault = sl.box(named(dom, "tf2")), sl.box(named(dom, "vault2"))
        self.assertEqual(tf[1], vault[1])                                     # one top line
        self.assertEqual(vault[0] - tf[0] - tf[2], layout.CARD_ICON_GAP)      # 13.5 pt apart
        tops = {sl.box(s)[1] for s in slots}
        self.assertEqual(tops, {1397149})
        xs = [sl.box(s)[0] for s in slots]
        self.assertAlmostEqual(xs[1] - xs[0], xs[2] - xs[1], delta=1)        # 1 EMU of rounding

    def test_rows_are_aligned_separately(self):
        dom = slide(card((400000, 1000000, 2500000, 1300000)), card((3300000, 1010000, 2500000, 1300000)),
                    card((400000, 3000000, 2500000, 1300000)), card((3300000, 3020000, 2500000, 1300000)))
        slots = sl.webinar_slots(dom)
        layout.align_cards(slots)
        self.assertEqual(sorted({sl.box(s)[1] for s in slots}), [1000000, 3000000])


class Highlights(unittest.TestCase):
    def test_gap_between_highlights(self):
        self.assertEqual(layout.highlight_gap_pt(6.0, 0.0), 6.0)    # Vault slides: highlights had no gap
        self.assertEqual(layout.highlight_gap_pt(6.0, 6.0), 0.0)    # Terraform slides: already 6 pt after
        self.assertEqual(layout.highlight_gap_pt(0.0, 0.0), layout.MIN_HIGHLIGHT_GAP_PT)


if __name__ == "__main__":
    unittest.main()
