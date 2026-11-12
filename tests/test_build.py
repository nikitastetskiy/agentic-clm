"""End to end, offline: plan + build + verify a deck from the frozen snapshot — then break the deck in
one way at a time and make sure the gate notices each one.

A gate that has only ever said PASS proves nothing. Every check in verify_build.py is paired here with
a deliberately damaged deck that it, specifically, must reject.
"""
import json
import re
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.dom import minidom

import helpers

def nudge(xml: str, anchor: str, *, dx: int = 0, dy: int = 0, dcy: int = 0) -> str:
    """Move (or grow) the first shape after `anchor` (its name or alt text): edits the next
    <a:off>/<a:ext> pair in the XML, which is that shape's own transform."""
    m = re.compile(r'<a:off x="(-?\d+)" y="(-?\d+)"/><a:ext cx="(\d+)" cy="(\d+)"/>').search(xml, xml.index(anchor))
    x, y, cx, cy = (int(v) for v in m.groups())
    return (xml[:m.start()] + f'<a:off x="{x + dx}" y="{y + dy}"/><a:ext cx="{cx}" cy="{cy + dcy}"/>'
            + xml[m.end():])


CHECKS = ("package", "columns", "blogs", "events", "fit", "layout", "links", "disclosure", "formatting",
          "freshness")


class WeeklyBuild(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="build-test-"))
        cls.env = helpers.snapshot_env(cls.tmp)
        cls.deck = cls.tmp / "deck.pptx"
        cls.plan_run = helpers.run("plan_deck.py", env=cls.env)
        cls.build_run = helpers.run("build_deck.py", "--out", str(cls.deck), env=cls.env)
        cls.plan = json.loads((cls.tmp / "data" / "deck-plan.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def verify(self, deck: Path, plan: Path | None = None) -> dict:
        report = self.tmp / f"verify-{deck.stem}.json"
        args = [str(deck), "--report", str(report)] + (["--plan", str(plan)] if plan else [])
        result = helpers.run("verify_build.py", *args, env=self.env)
        verdict = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(result.returncode == 0, verdict["ok"], result.stdout[-800:])
        return verdict

    def failed(self, verdict: dict) -> set:
        return {name for name, check in verdict["checks"].items() if not check["ok"]}

    def damaged(self, name: str, role: str, edit, *, rels: bool = False) -> Path:
        """A copy of the built deck with one slide (or its relationships) edited."""
        member = helpers.slide_member(self.deck, role)
        if rels:
            member = member.replace("ppt/slides/", "ppt/slides/_rels/") + ".rels"
        out = self.tmp / f"{name}.pptx"
        helpers.rewrite(self.deck, out, member, edit)
        return out

    # ---- the build itself ------------------------------------------------------------------------
    def test_plan_and_build_succeed(self):
        self.assertEqual(self.plan_run.returncode, 0, self.plan_run.stderr[-800:])
        self.assertEqual(self.build_run.returncode, 0, self.build_run.stderr[-800:])
        self.assertEqual(self.plan["build"]["status"], "ok")

    def test_the_built_deck_passes_every_check(self):
        verdict = self.verify(self.deck)
        self.assertEqual(tuple(verdict["checks"]), CHECKS)
        self.assertEqual(self.failed(verdict), set(), verdict["checks"])

    def test_the_deck_holds_the_automated_slides_in_order(self):
        report = json.loads((self.tmp / "data" / "build-report.json").read_text(encoding="utf-8"))
        self.assertEqual([s["role"] for s in report["slides"]],
                         ["cover", "terraform-core-releases", "terraform-enterprise-releases",
                          "hcp-terraform-changelog", "terraform-blogs", "terraform-webinars",
                          "vault-enterprise-releases", "hcp-vault-changelog", "vault-blogs", "vault-webinars",
                          "featured-event"])
        with zipfile.ZipFile(self.deck) as z:
            slides = [n for n in z.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")]
            self.assertEqual(len(slides), 11)
            self.assertFalse([n for n in z.namelist() if "notesSlide" in n])   # no speaker notes travel

    def test_month_columns_follow_the_data(self):
        columns = {s["role"]: [c["header"] for c in s["columns"]] for s in self.plan["slides"] if s["kind"] == "columns"}
        # By October 13 only a 1.17.0 release candidate had shipped that month, and pre-releases are not listed.
        self.assertEqual(columns["terraform-core-releases"], ["July", "August", "September"])
        self.assertEqual(columns["terraform-enterprise-releases"], ["July", "August", "September"])
        self.assertEqual(columns["vault-enterprise-releases"], ["August", "September", "October"])

    def test_the_same_inputs_build_the_same_file(self):
        again = self.tmp / "again.pptx"
        self.assertEqual(helpers.run("build_deck.py", "--out", str(again), env=self.env).returncode, 0)
        self.assertEqual(self.deck.read_bytes(), again.read_bytes())

    def test_a_week_without_events_still_builds_and_says_so(self):
        work = Path(tempfile.mkdtemp(prefix="build-noevents-"))
        try:
            env = helpers.snapshot_env(work)
            (work / "data" / "events.json").write_text(json.dumps(
                {"status": "unavailable", "note": "render failed", "featured": [],
                 "webinars": {"terraform": [], "vault": [], "other": []}}), encoding="utf-8")
            self.assertEqual(helpers.run("plan_deck.py", env=env).returncode, 0)
            self.assertEqual(helpers.run("build_deck.py", "--out", str(work / "d.pptx"), env=env).returncode, 0)
            result = helpers.run("verify_build.py", str(work / "d.pptx"), "--report", str(work / "v.json"), env=env)
            self.assertEqual(result.returncode, 0, result.stdout[-800:])
            self.assertIn("degraded", result.stdout)
            plan = json.loads((work / "data" / "deck-plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["build"]["status"], "degraded")
            self.assertEqual({o["role"] for o in plan["omitted"]},
                             {"terraform-webinars", "vault-webinars", "featured-event"})
            with zipfile.ZipFile(work / "d.pptx") as z:   # the event slides are gone, not left stale
                self.assertEqual(len([n for n in z.namelist() if n.startswith("ppt/slides/slide")]), 8)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    # ---- one damaged deck per check ----------------------------------------------------------------
    def test_gate_catches_a_wrong_version(self):
        deck = self.damaged("wrong-version", "terraform-core-releases",
                            lambda x: x.replace("v1.16.5 (2026-09-30)", "v1.16.9 (2026-09-30)"))
        self.assertIn("columns", self.failed(self.verify(deck)))

    def test_gate_catches_a_swapped_link(self):
        deck = self.damaged("swapped-link", "terraform-core-releases",
                            lambda x: x.replace("releases/tag/v1.16.5", "releases/tag/v1.16.4"), rels=True)
        self.assertIn("columns", self.failed(self.verify(deck)))

    def test_gate_catches_an_internal_link(self):
        deck = self.damaged("internal-link", "terraform-blogs",
                            lambda x: x.replace("https://www.hashicorp.com/en/blog/products/terraform",
                                                "https://w3.ibm.com/internal/blogs"), rels=True)
        self.assertEqual(self.failed(self.verify(deck)), {"links"})

    def test_gate_catches_overflowing_text(self):
        long = "Improves deployment reliability in a great many ways that go on and on. " * 12
        deck = self.damaged("overflow", "terraform-enterprise-releases",
                            lambda x: x.replace("Improves deployment reliability", long + "Improves deployment reliability", 1))
        self.assertIn("fit", self.failed(self.verify(deck)))

    def test_gate_catches_an_undisclosed_cve(self):
        deck = self.damaged("cve", "vault-enterprise-releases",
                            lambda x: x.replace("Bug fixes and security patches",
                                                "Bug fixes and security patches for CVE-2099-12345", 1))
        self.assertIn("disclosure", self.failed(self.verify(deck)))

    def test_gate_catches_foreign_formatting(self):
        deck = self.damaged("colour", "hcp-vault-changelog", lambda x: x.replace('val="F9B373"', 'val="FF00AA"', 1))
        self.assertEqual(self.failed(self.verify(deck)), {"formatting"})

    def test_gate_catches_comment_authors(self):
        deck = self.tmp / "authors.pptx"
        with zipfile.ZipFile(self.deck) as src, zipfile.ZipFile(deck, "w", zipfile.ZIP_DEFLATED) as out:
            for info in src.infolist():
                out.writestr(info, src.read(info))
            out.writestr("ppt/authors.xml", '<p188:authorLst xmlns:p188="http://schemas.microsoft.com/office/'
                                            'powerpoint/2018/8/main"><p188:author name="A Person"/></p188:authorLst>')
        self.assertEqual(self.failed(self.verify(deck)), {"disclosure"})

    def test_gate_catches_literal_bold_markup(self):
        deck = self.damaged("markup", "vault-enterprise-releases",
                            lambda x: x.replace("Bug fixes and security patches", "Bug fixes and **security** patches", 1))
        self.assertIn("formatting", self.failed(self.verify(deck)))

    def test_gate_catches_a_font_embedded_under_another_name(self):
        # The reference deck's fault: PowerPoint draws "Inter Medium" text with this data when the
        # typeface is not installed, so it must be Inter Medium — here it is made plain Inter.
        with zipfile.ZipFile(self.deck) as z:
            pres = z.read("ppt/presentation.xml").decode("utf-8")
        rid = {face: re.search(r'<p:regular r:id="(\w+)"', pres[pres.index(f'typeface="{face}"'):]).group(1)
               for face in ("Inter", "Inter Medium")}

        def swap(rels: str) -> str:
            dom = minidom.parseString(rels)
            by_id = {r.getAttribute("Id"): r for r in dom.getElementsByTagName("Relationship")}
            by_id[rid["Inter Medium"]].setAttribute("Target", by_id[rid["Inter"]].getAttribute("Target"))
            return dom.toxml()

        deck = self.tmp / "font-name.pptx"
        helpers.rewrite(self.deck, deck, "ppt/_rels/presentation.xml.rels", swap)
        verdict = self.verify(deck)
        self.assertEqual(self.failed(verdict), {"formatting"})
        self.assertIn("embedded font 'Inter Medium' (regular) carries 'Inter' data",
                      " ".join(verdict["checks"]["formatting"]["notes"]))

    def test_gate_catches_a_cover_that_claims_readiness(self):
        deck = self.damaged("cover", "cover", lambda x: x.replace("AUTO-GENERATED: REVIEW BEFORE SHARING",
                                                                  "CURRENT STATUS: READY TO PUBLISH"))
        self.assertEqual(self.failed(self.verify(deck)), {"freshness"})

    def test_gate_catches_a_hidden_slide(self):
        deck = self.damaged("hidden", "vault-blogs", lambda x: x.replace("<p:sld ", '<p:sld show="0" ', 1))
        self.assertEqual(self.failed(self.verify(deck)), {"package"})

    def test_gate_catches_a_duplicated_blog_post(self):
        plan = self.plan["slides"][[s["role"] for s in self.plan["slides"]].index("vault-blogs")]
        first, second = plan["entries"][0]["url"], plan["entries"][1]["url"]
        deck = self.damaged("dup-blog", "vault-blogs", lambda x: x.replace(second, first), rels=True)
        self.assertIn("blogs", self.failed(self.verify(deck)))

    def test_gate_catches_an_event_that_has_already_happened(self):
        stale = json.loads(json.dumps(self.plan))
        stale["build"]["date"] = "2026-11-30"        # the deck's November events are now in the past
        path = self.tmp / "stale-plan.json"
        path.write_text(json.dumps(stale), encoding="utf-8")
        self.assertIn("events", self.failed(self.verify(self.deck, plan=path)))

    def test_gate_catches_a_card_without_its_description(self):
        slot = self.plan["slides"][[s["role"] for s in self.plan["slides"]].index("vault-webinars")]["slots"][2]
        report = json.loads((self.tmp / "data" / "build-report.json").read_text(encoding="utf-8"))
        shown = next(s for s in report["slides"] if s["role"] == "vault-webinars")["events"][2]["description"]
        self.assertTrue(shown and slot["title"])
        deck = self.damaged("no-description", "vault-webinars", lambda x: x.replace(shown, " "))
        self.assertIn("events", self.failed(self.verify(deck)))

    # ---- layout: the slips a reviewer corrected by hand on 2026-10-14, one damaged deck each -------
    def test_icons_follow_the_reviewers_correction(self):
        """On a card for both products the Vault icon sits on the Terraform icon's top line, 13.5 pt to its
        right, and the Terraform icon 13.5 pt from the card's edge — where a reviewer moved it by hand."""
        from xml.dom import minidom
        from lib import slides as sl
        dom = minidom.parseString(helpers.slide_text(self.deck, "vault-webinars"))
        card = sl.webinar_slots(dom)[1]                       # Dec 8: badged Terraform + Vault
        icons = sorted((d for d in sl.slot_decorations(dom, card) if not sl.is_flag(d)), key=lambda d: sl.box(d)[0])
        (tx, ty, tw, _), (vx, vy, _, _) = (sl.box(i) for i in icons)
        self.assertEqual(ty, vy)
        self.assertAlmostEqual((vx - tx - tw) / 12700, 13.5, places=1)
        self.assertAlmostEqual((tx - sl.box(card)[0]) / 12700, 13.5, places=1)

    def test_gate_catches_an_icon_off_the_cards_icon_line(self):
        deck = self.damaged("icon-line", "vault-webinars", lambda x: nudge(x, 'name="Vault icon"', dy=-48260))
        self.assertEqual(self.failed(self.verify(deck)), {"layout"})

    def test_gate_catches_icons_crowding_each_other(self):
        xml = helpers.slide_text(self.deck, "vault-webinars")
        second_vault = [m.start() for m in re.finditer('name="Vault icon"', xml)][1]   # the card with two icons
        deck = self.damaged("icon-gap", "vault-webinars",
                            lambda x: x[:second_vault] + nudge(x[second_vault:], 'name="Vault icon"', dx=-120650))
        self.assertEqual(self.failed(self.verify(deck)), {"layout"})

    def test_gate_catches_a_flag_off_its_corner(self):
        deck = self.damaged("flag", "vault-webinars", lambda x: nudge(x, 'descr="Union Jack', dy=-29210))
        self.assertEqual(self.failed(self.verify(deck)), {"layout"})

    def test_gate_catches_cards_off_one_line(self):
        deck = self.damaged("card-line", "terraform-webinars",
                            lambda x: nudge(x, 'name="Google Shape;397;p78"', dy=19050))
        self.assertEqual(self.failed(self.verify(deck)), {"layout"})

    def test_more_events_than_a_row_get_a_second_row_from_the_left(self):
        from lib import slides as sl
        from lib.layout import CARD_BOTTOM, card_rows
        dom = minidom.parseString(helpers.slide_text(self.deck, "terraform-webinars"))
        first, second = card_rows(sl.webinar_slots(dom))                  # the snapshot's 4 Terraform events
        (x0, y, cx, cy), (x1, *_), _ = (sl.box(c) for c in first)
        self.assertEqual(len(second), 1)
        x, y2, w, h = sl.box(second[0])
        self.assertEqual((x, w, h), (x0, cx, cy))                         # under the first card, one size
        self.assertAlmostEqual(y2 - y - cy, x1 - x0 - cx, delta=1)        # as far below as the cards are apart
        with zipfile.ZipFile(self.deck) as z:
            size = minidom.parseString(z.read("ppt/presentation.xml")).getElementsByTagName("p:sldSz")[0]
        self.assertLessEqual(y2 + cy, int(size.getAttribute("cy")) - CARD_BOTTOM)   # 33 pt above the bottom

    def test_gate_catches_a_second_row_off_its_place(self):
        xml = helpers.slide_text(self.deck, "terraform-webinars")
        last = xml.rindex('name="Google Shape;397;p78"')                      # the card copied for the 4th event
        deck = self.damaged("row-off-place", "terraform-webinars",
                            lambda x: x[:last] + nudge(x[last:], 'name="Google Shape;397;p78"', dx=-63500))
        self.assertEqual(self.failed(self.verify(deck)), {"layout"})

    def test_gate_catches_a_description_against_its_title(self):
        gap = '<a:spcBef><a:spcPts val="600"/></a:spcBef>'                   # the reference deck had none
        deck = self.damaged("no-title-gap", "vault-webinars", lambda x: x.replace(gap, "", 1))
        self.assertEqual(self.failed(self.verify(deck)), {"layout"})

    def test_hcp_slides_take_their_products_bullet(self):
        """The reference deck's HCP Vault bullets were black and its HCP Terraform highlights had none."""
        for hcp, release in (("hcp-vault-changelog", "vault-enterprise-releases"),
                             ("hcp-terraform-changelog", "terraform-enterprise-releases")):
            colours = [set(re.findall(r'<a:buClr><a:srgbClr val="(\w+)"/>', helpers.slide_text(self.deck, role)))
                       for role in (hcp, release)]
            self.assertEqual(colours[0], colours[1], hcp)
            self.assertEqual(len(colours[0]), 1, hcp)

    def test_every_release_slide_spaces_its_entries_like_hcp_vault(self):
        """The reference deck spaced and sized each release slide's entries its own way (deck.entries_from
        names the one to follow), its boxes grew to different heights, and an unsized end mark lifted
        one month header above the others."""
        from lib import compose, pptx_edit as px, slides as sl, textfit
        report = json.loads((self.tmp / "data" / "build-report.json").read_text(encoding="utf-8"))
        spacing = {s["role"]: s["spacing"] for s in report["slides"] if "spacing" in s}
        looks, boxes = {}, set()
        for role in spacing:
            cols = sl.columns(minidom.parseString(helpers.slide_text(self.deck, role)))
            looks[role] = {(sl.is_heading(p), compose.style_of(p, default_size=6.5))
                           for c in cols for p in px.paragraphs(c["body"]) if sl.para_text(p).strip()}
            boxes |= {sl.box(c["body"])[3] for c in cols}
            for c in cols:   # a month header's end mark is set like its text
                p = px.paragraphs(c["header"])[0]
                text = p.getElementsByTagName("a:rPr")[0].toxml()
                for end in p.getElementsByTagName("a:endParaRPr"):
                    self.assertEqual(end.toxml().replace("a:endParaRPr", "a:rPr"), text, role)

        def look(styles, mode: str) -> set:   # a slide that cannot hold a month tightens its spacing
            return {(h, s.size_pt, s.line_pct, s.before_pt, s.after_pt)
                    for h, s in ((h, textfit.spaced(s, mode)) for h, s in styles)}
        self.assertEqual(spacing.pop("hcp-vault-changelog"), "template")
        reference = looks["hcp-vault-changelog"]
        self.assertEqual(len(reference), 2)
        for role, mode in spacing.items():
            self.assertEqual(look(looks[role], "template"), look(reference, mode), role)
        self.assertEqual(len(spacing), 4)
        self.assertEqual(len(boxes), 1)

    def test_topic_labels_are_links_without_a_visible_underline(self):
        """PowerPoint underlines a link whatever its u= says, so the underline has no fill."""
        from lib import slides as sl
        for role in ("terraform-blogs", "vault-blogs"):
            buttons = sl.link_buttons(minidom.parseString(helpers.slide_text(self.deck, role)))
            self.assertTrue(buttons, role)
            for sp in buttons:
                page = sp.getElementsByTagName("p:cNvPr")[0].getElementsByTagName("a:hlinkClick")[0]
                for rpr in sp.getElementsByTagName("a:rPr"):
                    link = rpr.getElementsByTagName("a:hlinkClick")[0]
                    self.assertEqual(link.getAttribute("r:id"), page.getAttribute("r:id"))
                    self.assertEqual(rpr.getAttribute("u"), "none")
                    self.assertTrue(rpr.getElementsByTagName("a:uFill")[0].getElementsByTagName("a:noFill"))
                    self.assertEqual(link.getElementsByTagName("ahyp:hlinkClr")[0].getAttribute("val"), "tx")

    def test_the_event_points_sit_level_with_the_picture(self):
        from lib import slides as sl
        dom = minidom.parseString(helpers.slide_text(self.deck, "featured-event"))
        self.assertEqual(sl.featured_parts(dom)["description"].getElementsByTagName("a:bodyPr")[0]
                         .getAttribute("anchor"), "ctr")

    def test_gate_catches_a_topic_button_that_overflows(self):
        def unscaled(xml: str) -> str:                       # the widest label at its full 7 pt again
            at = xml.rindex('sz="', 0, xml.index(">Culture &amp; Collaboration<"))
            return xml[:at] + 'sz="700"' + xml[xml.index('"', at + 4) + 1:]
        deck = self.damaged("button", "vault-blogs", unscaled)
        self.assertEqual(self.failed(self.verify(deck)), {"fit"})

    def test_gate_catches_a_link_that_underlines_a_space(self):
        deck = self.damaged("link-space", "vault-webinars", lambda x: x.replace(">here</a:t>", ">here </a:t>", 1))
        self.assertEqual(self.failed(self.verify(deck)), {"links"})

    def test_gate_catches_a_picture_past_the_slide_edge(self):
        deck = self.damaged("overhang", "cover", lambda x: nudge(x, 'name="Picture 11"', dcy=106045))
        self.assertEqual(self.failed(self.verify(deck)), {"layout"})

    def test_gate_catches_highlights_that_run_together(self):
        apart = '<a:spcAft><a:spcPts val="600"/></a:spcAft><a:buClr>'   # a highlight's space after
        deck = self.damaged("no-gap", "vault-enterprise-releases",
                            lambda x: x.replace(apart, apart.replace('val="600"', 'val="0"')))
        self.assertEqual(self.failed(self.verify(deck)), {"layout"})

    def test_gate_catches_a_month_header_off_its_line(self):
        deck = self.damaged("header", "hcp-terraform-changelog",
                            lambda x: nudge(x, 'name="Google Shape;1262;p168"', dy=10160))
        self.assertEqual(self.failed(self.verify(deck)), {"layout"})

    def test_gate_catches_a_blog_entry_off_the_grid(self):
        deck = self.damaged("blog-grid", "vault-blogs", lambda x: nudge(x, 'name="Group 5"', dx=-33020))
        self.assertEqual(self.failed(self.verify(deck)), {"layout"})

    def test_gate_catches_a_blog_entry_of_another_height(self):
        deck = self.damaged("blog-height", "vault-blogs", lambda x: nudge(x, 'name="Group 5"', dcy=139700))
        self.assertEqual(self.failed(self.verify(deck)), {"layout"})

    def test_gate_catches_a_subtitle_off_the_other_slides_line(self):
        deck = self.damaged("subtitle", "vault-webinars", lambda x: nudge(x, 'name="Subtitle 2"', dy=38100))
        self.assertEqual(self.failed(self.verify(deck)), {"layout"})

    def slide_cx(self) -> int:
        with zipfile.ZipFile(self.deck) as z:
            return int(re.search(r'<p:sldSz cx="(\d+)"', z.read("ppt/presentation.xml").decode()).group(1))

    def test_a_slide_placed_like_another_sits_where_it_does(self):
        from lib import slides as sl
        from lib.layout import card_rows
        cx = self.slide_cx()

        def cards(role):     # x, y and width of the first row (a second row shortens every card)
            dom = minidom.parseString(helpers.slide_text(self.deck, role))
            return [sl.box(c)[:3] for c in card_rows(sl.webinar_slots(dom))[0]]

        def entries(role):
            dom = minidom.parseString(helpers.slide_text(self.deck, role))
            return [sl.box(g["group"])[:2] for g in sl.blog_groups(dom, cx)]
        self.assertEqual(cards("terraform-webinars"), cards("vault-webinars"))
        self.assertEqual(entries("terraform-blogs"), entries("vault-blogs"))

    def test_gate_catches_a_slide_off_its_siblings_places(self):
        from lib import slides as sl
        cx = self.slide_cx()

        def lower(xml):      # every entry 2 pt lower: still a square grid, no longer the Vault slide's
            dom = minidom.parseString(xml)
            for g in sl.blog_groups(dom, cx):
                sl.set_box(g["group"], y=sl.box(g["group"])[1] + 25400)
            return dom.toxml()
        verdict = self.verify(self.damaged("sibling", "terraform-blogs", lower))
        self.assertEqual(self.failed(verdict), {"layout"})
        self.assertIn("vault-blogs", " ".join(verdict["checks"]["layout"]["notes"]))


class CardDescriptions(unittest.TestCase):
    LONG = ("Join this hands-on workshop to work directly with HashiCorp Vault in an Amazon EKS environment and "
            "learn how to secure agentic AI applications with identity-based access and just-in-time credentials.")
    ABSTRACT = ("AI agents introduce a new set of identity and access challenges. They can act independently, on "
                "behalf of users and across multiple systems, which makes standing privileges hard to audit.")

    def describe(self, options, lines):
        import build_deck
        from lib.textfit import ParaStyle
        return build_deck.card_description(options, ParaStyle(size_pt=6.5), 185, lines)

    def test_an_option_that_ends_with_its_sentence_is_preferred_to_a_cut_one(self):
        self.assertEqual(self.describe([self.LONG, "", self.ABSTRACT], 6), self.LONG)       # a tall card
        self.assertEqual(self.describe([self.LONG, "", self.ABSTRACT], 2),                  # a short one
                         "AI agents introduce a new set of identity and access challenges.")

    def test_only_when_no_option_fits_is_one_cut(self):
        cut = self.describe([self.LONG], 2)
        self.assertTrue(cut.endswith("…") and self.LONG.startswith(cut[:-1]), cut)


class BlogTitles(unittest.TestCase):
    def test_emphasis_never_costs_a_title_its_text(self):
        import build_deck
        from lib import textfit
        style = textfit.ParaStyle(size_pt=8.0)
        title = "Announcing the public preview of HashiCorp Secure Hub: Govern Vault at scale"
        marked = f"**{title}**"                                # bold all through: wider than plain
        width = next(w / 2 for w in range(100, 800)
                     if textfit.line_count([(title, False)], 8.0, w / 2) <= 2 < textfit.line_count([(title, True)], 8.0, w / 2))
        self.assertEqual(build_deck.blog_title(marked, style, width), (title, 2))
        self.assertEqual(build_deck.blog_title(marked, style, width * 2)[0], marked)


if __name__ == "__main__":
    unittest.main()
