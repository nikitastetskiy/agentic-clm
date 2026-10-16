"""The text-fitting model, checked against layout decisions PowerPoint itself made.

The template still carries the reference deck's blog slides. Their entry boxes are "resize shape to
fit text", so each box's height records how many lines PowerPoint gave the title when a person typed
it: a one-line box or a taller two-line box. If the model predicts those line counts, its glyph
widths and wrapping agree with PowerPoint's on real titles.
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from xml.dom import minidom

import helpers

from lib import compose, deck, slides as sl, textfit
from lib import pptx_edit as px
from lib.textfit import ParaStyle

TEMPLATE = helpers.REPO / "templates" / "cadence-template.pptx"


class AgainstPowerPoint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="fit-test-"))
        px.unpack(TEMPLATE, cls.tmp)
        cls.roles = deck.slides_by_role(cls.tmp)
        pres = minidom.parse(str(cls.tmp / "ppt" / "presentation.xml"))
        cls.slide_cx = int(pres.getElementsByTagName("p:sldSz")[0].getAttribute("cx"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_blog_titles_wrap_where_powerpoint_wrapped_them(self):
        checked = 0
        for role in ("terraform-blogs", "vault-blogs"):
            dom = minidom.parse(str(self.roles[role][0]))
            groups = sl.blog_groups(dom, self.slide_cx)
            one_line = min(sl.box(g["box"])[3] for g in groups)
            for g in groups:
                title = px.paragraphs(g["box"])[1]
                left, _t, right, _b = compose.insets(g["box"])
                width = (sl.box(g["box"])[2] - left - right) / textfit.EMU_PT
                segs = [(px.run_text(r), sl.is_bold(r)) for r in px.para_runs(title)]
                powerpoint_lines = 1 if sl.box(g["box"])[3] - one_line < 60000 else 2
                self.assertEqual(textfit.line_count(segs, compose.style_of(title).size_pt, width),
                                 powerpoint_lines, sl.para_text(title))
                checked += 1
        self.assertEqual(checked, 16)

    def test_every_automated_slide_structure_is_found_in_the_template(self):
        for role in ("terraform-core-releases", "terraform-enterprise-releases", "hcp-terraform-changelog",
                     "vault-enterprise-releases", "hcp-vault-changelog"):
            dom = minidom.parse(str(self.roles[role][0]))
            cols = sl.columns(dom)
            self.assertEqual(len(cols), 3, role)
            self.assertTrue(all(sl.header_text(c["header"]) for c in cols), role)
        for role in ("terraform-webinars", "vault-webinars"):
            self.assertEqual(len(sl.webinar_slots(minidom.parse(str(self.roles[role][0])))), 3, role)
        parts = sl.featured_parts(minidom.parse(str(self.roles["featured-event"][0])))
        self.assertTrue(all(parts.get(k) is not None for k in ("title", "subtitle", "description")))


class Wrapping(unittest.TestCase):
    STYLE = ParaStyle(size_pt=6.5, after_pt=6.0, indent_pt=13.5)

    def test_more_text_or_less_room_never_needs_fewer_lines(self):
        text = "Strengthens VCS integrations and Terraform 1.15 compatibility while improving drift detection."
        narrow = textfit.line_count([(text, False)], 6.5, 120)
        self.assertGreaterEqual(narrow, textfit.line_count([(text, False)], 6.5, 180))
        self.assertGreaterEqual(textfit.line_count([(text + " " + text, False)], 6.5, 120), narrow)
        self.assertGreaterEqual(textfit.line_count([(text, True)], 6.5, 120), narrow)   # bold is wider

    def test_paragraph_height_counts_lines_and_spacing(self):
        one = textfit.para_height([("Bug fixes", False)], self.STYLE, 185)
        self.assertAlmostEqual(one, 6.5 * textfit.LINE_FACTOR + 6.0)
        tight = textfit.spaced(ParaStyle(size_pt=8, line_pct=1.5, after_pt=6), "tight")
        self.assertEqual((tight.line_pct, tight.after_pt), (1.0, 3.0))
        self.assertEqual(textfit.spaced(ParaStyle(size_pt=8, line_pct=1.5), "template").line_pct, 1.5)

    def test_prose_is_cut_between_sentences(self):
        text = ("Automation has grown from simple scripts to whole environments. In this webinar learn how "
                "Terraform manages everything as code. A third sentence that will not fit either.")
        fitted = textfit.fit_text(text, ParaStyle(size_pt=6.5), 185, 2)
        self.assertEqual(fitted, "Automation has grown from simple scripts to whole environments.")
        self.assertEqual(textfit.fit_text(text, ParaStyle(size_pt=6.5), 185, 20), text)

    def test_a_title_is_cut_at_a_word_with_an_ellipsis(self):
        title = "Simplify compliance with the native pre-written policy experience. In HCP Terraform and beyond"
        fitted = textfit.fit_text(title, ParaStyle(size_pt=8), 150, 1, whole_sentences=False)
        self.assertTrue(fitted.endswith("…"))
        self.assertTrue(title.startswith(fitted[:-1]))
        self.assertEqual(textfit.line_count([(fitted, False)], 8, 150), 1)


if __name__ == "__main__":
    unittest.main()
