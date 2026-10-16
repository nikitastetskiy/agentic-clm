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
        self.assertEqual(columns["terraform-core-releases"], ["August", "September", "October"])
        self.assertEqual(columns["terraform-enterprise-releases"], ["July", "August", "September"])
        self.assertEqual(columns["vault-enterprise-releases"], ["August", "September", "October"])

    def test_the_same_inputs_build_the_same_slides(self):
        again = self.tmp / "again.pptx"
        self.assertEqual(helpers.run("build_deck.py", "--out", str(again), env=self.env).returncode, 0)
        with zipfile.ZipFile(self.deck) as a, zipfile.ZipFile(again) as b:
            names = sorted(n for n in a.namelist() if n.startswith("ppt/slides/"))
            self.assertEqual(names, sorted(n for n in b.namelist() if n.startswith("ppt/slides/")))
            for name in names:
                self.assertEqual(a.read(name), b.read(name), name)

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

    def test_gate_catches_a_picture_past_the_slide_edge(self):
        deck = self.damaged("overhang", "cover", lambda x: nudge(x, 'name="Picture 11"', dcy=106045))
        self.assertEqual(self.failed(self.verify(deck)), {"layout"})

    def test_gate_catches_highlights_that_run_together(self):
        follow = '<a:spcBef><a:spcPts val="600"/></a:spcBef><a:spcAft><a:spcPts val="0"/>'
        deck = self.damaged("no-gap", "vault-enterprise-releases",
                            lambda x: x.replace(follow, follow.replace('val="600"', 'val="0"', 1)))
        self.assertEqual(self.failed(self.verify(deck)), {"layout"})

    def test_gate_catches_a_month_header_off_its_line(self):
        deck = self.damaged("header", "hcp-terraform-changelog",
                            lambda x: nudge(x, 'name="Google Shape;1262;p168"', dy=10160))
        self.assertEqual(self.failed(self.verify(deck)), {"layout"})

    def test_gate_catches_a_blog_entry_off_the_grid(self):
        deck = self.damaged("blog-grid", "vault-blogs", lambda x: nudge(x, 'name="Group 5"', dx=-33020))
        self.assertEqual(self.failed(self.verify(deck)), {"layout"})


if __name__ == "__main__":
    unittest.main()
