"""The planner's rules: which months, which entries, which wording — on small hand-made data."""
import unittest
from datetime import date

import helpers  # noqa: F401

import plan_deck


def release(version, day, *, pre=False, ent_only=False, sections=None, line=None):
    return {"version": version, "date": day, "is_prerelease": pre, "enterprise_only": ent_only,
            "line": line or ".".join(version.split(".")[:2]), "url": f"https://example.test/{version}",
            "sections": sections or {}}


def note(text, lead=""):
    return {"lead": lead, "text": text, "enterprise": False}


FIXES = {"BUG FIXES": [note("Fixed a crash.")]}
MINOR = {"NEW FEATURES": [note("Imports now work inside child modules. More detail follows.")],
         "ENHANCEMENTS": [note("Faster plans.")], "BUG FIXES": [note("Fixed a crash.")]}


def planner(*, slides, releases=None, changelogs=None, blogs=None, events=None, enrich=None, columns=3):
    data = {"releases": releases or {"products": {}},
            "changelogs": changelogs or {"products": {}},
            "blogs": blogs or {"products": {}},
            "events": events or {"status": "ok", "webinars": {"terraform": [], "vault": [], "other": []},
                                 "featured": []}}
    cfg = {"deck": {"title": "T", "columns": columns, "cover_status": "REVIEW"}, "slides": slides}
    return plan_deck.Planner(cfg, data, enrich or {}, date(2026, 10, 13))


TF_RULE = {"role": "tf", "kind": "columns", "source": {"type": "releases", "product": "terraform"},
           "style": {"wording": "label", "prerelease_label": "Pre-release"}}
VAULT_RULE = {"role": "vault", "kind": "columns", "source": {"type": "releases", "product": "vault"},
              "style": {"group_same_day": True, "lts_label": "Bug fixes and improvements"}}


class Labels(unittest.TestCase):
    def test_category_labels(self):
        self.assertEqual(plan_deck.section_label(FIXES), "Bug fixes")
        self.assertEqual(plan_deck.section_label(MINOR), "New features, enhancements and bug fixes")
        self.assertEqual(plan_deck.section_label({"Bug fixes and security patches": [note("x")]}),
                         "Bug fixes and security patches")
        self.assertEqual(plan_deck.section_label({"Bug Fixes": [note("x")], "Security": [note("y")]}),
                         "Bug fixes and security updates")
        self.assertEqual(plan_deck.section_label({}), "Maintenance release")

    def test_lead_sentence_is_what_a_release_adds(self):
        self.assertEqual(plan_deck.lead_sentence(MINOR), "Imports now work inside child modules.")
        self.assertEqual(plan_deck.lead_sentence({"Improvements": [note("Now faster.", lead="Planner")]}),
                         "**Planner:** Now faster.")

    def test_a_fix_only_release_has_no_lead_sentence(self):
        # quoting one arbitrary fix would misrepresent a maintenance or security release
        self.assertIsNone(plan_deck.lead_sentence(FIXES))
        self.assertIsNone(plan_deck.lead_sentence({"Security": [note(
            "Security vulnerabilities have been addressed and resolved in this update.")]}))


class ReleaseColumns(unittest.TestCase):
    def plan(self, rels, rule=TF_RULE, enrich=None, columns=3):
        p = planner(slides=[rule], releases={"products": {rule["source"]["product"]: {"releases": rels}}},
                    enrich=enrich, columns=columns)
        return p, p.build()["slides"][0]

    def test_the_newest_three_months_that_have_releases(self):
        rels = [release("1.17.0-rc1", "2026-10-07", pre=True), release("1.16.5", "2026-09-30", sections=FIXES),
                release("1.15.8", "2026-06-08", sections=FIXES), release("1.15.0", "2026-04-29", sections=MINOR)]
        _p, slide = self.plan(rels)
        # July and August have no release: the window reaches back to June rather than show an
        # empty column, and April is one month too many
        self.assertEqual([c["header"] for c in slide["columns"]], ["June", "September", "October"])
        _p, two = self.plan(rels, columns=2)
        self.assertEqual([c["month"] for c in two["columns"]], ["2026-09", "2026-10"])

    def test_entries_are_newest_first_with_their_links(self):
        rels = [release("1.16.3", "2026-09-16", sections=FIXES), release("1.16.5", "2026-09-30", sections=FIXES),
                release("1.17.0-beta1", "2026-09-09", pre=True)]
        _p, slide = self.plan(rels)
        entries = slide["columns"][0]["entries"]
        self.assertEqual([e["heading"][0]["text"] for e in entries],
                         ["v1.16.5 (2026-09-30)", "v1.16.3 (2026-09-16)", "v1.17.0-beta1 (2026-09-09)"])
        self.assertEqual(entries[0]["heading"][0]["url"], "https://example.test/1.16.5")

    def test_prereleases_and_label_only_wording(self):
        rels = [release("1.17.0-alpha20260827", "2026-08-27", pre=True), release("1.16.0", "2026-08-26", sections=MINOR)]
        _p, slide = self.plan(rels)
        alpha, minor = slide["columns"][0]["entries"]
        self.assertEqual(alpha["heading"][0]["text"], "v1.17.0-alpha (2026-08-27)")   # date not shown twice
        self.assertEqual(alpha["options"], [["Pre-release"]])
        self.assertEqual(minor["options"], [["New features, enhancements and bug fixes"]])

    def test_rich_wording_then_label_when_the_slide_allows_sentences(self):
        rule = {**TF_RULE, "style": {}}
        _p, slide = self.plan([release("1.16.0", "2026-08-26", sections=MINOR),
                               release("1.16.1", "2026-08-28", sections=FIXES)], rule=rule)
        patch, minor = slide["columns"][0]["entries"]
        self.assertEqual(minor["options"], [["Imports now work inside child modules."],
                                            ["New features, enhancements and bug fixes"]])
        self.assertEqual(patch["options"], [["Bug fixes"]])

    def test_stored_wording_wins(self):
        enrich = {"highlights": {"terraform/1.16.0": {"bullets": ["A **curated** line.."], "source": "x"}}}
        _p, slide = self.plan([release("1.16.0", "2026-08-26", sections=MINOR)], rule={**TF_RULE, "style": {}},
                              enrich=enrich)
        self.assertEqual(slide["columns"][0]["entries"][0]["options"][0], ["A **curated** line."])

    def test_vault_versions_of_one_day_share_an_entry(self):
        rels = [release("2.1.2", "2026-10-07", sections={"Bug fixes and security patches": [note("x")]}),
                release("1.21.12", "2026-10-07", ent_only=True), release("1.19.23", "2026-10-07", ent_only=True),
                release("1.20.17", "2026-10-07", ent_only=True), release("1.21.9", "2026-08-04", ent_only=True)]
        _p, slide = self.plan(rels, rule=VAULT_RULE)
        august, october = slide["columns"]
        entry = october["entries"][0]
        self.assertEqual("".join(seg["text"] for seg in entry["heading"]),
                         "v2.1.2 / v1.21.12 / v1.20.17 / v1.19.23 (2026-10-07)")
        self.assertEqual([seg.get("url") for seg in entry["heading"] if seg.get("url")],
                         [f"https://example.test/{v}" for v in ("2.1.2", "1.21.12", "1.20.17", "1.19.23")])
        self.assertEqual(entry["key"], "vault/2.1.2")
        self.assertEqual(entry["options"], [["Bug fixes and security patches"]])
        self.assertEqual(august["entries"][0]["options"], [["Bug fixes and improvements"]])   # LTS only

    def test_missing_release_data_fails_the_plan(self):
        p = planner(slides=[TF_RULE])
        self.assertEqual(p.build()["build"]["status"], "failed")


class DisclosureGuard(unittest.TestCase):
    RULE = {"role": "tfe", "kind": "columns", "source": {"type": "releases", "product": "vault"}, "style": {}}

    def plan(self, bullet, sections):
        enrich = {"highlights": {"vault/2.0.0": {"bullets": [bullet], "source": "llm:test"}}}
        p = planner(slides=[self.RULE], enrich=enrich,
                    releases={"products": {"vault": {"releases": [release("2.0.0", "2026-10-01", sections=sections)]}}})
        return p, p.build()

    def test_a_cve_the_public_notes_name_may_be_shown(self):
        sections = {"Security": [note("Addresses CVE-2026-14468 in module ingestion.")]}
        _p, plan = self.plan("Hardens module ingestion, including CVE-2026-14468.", sections)
        self.assertEqual(plan["slides"][0]["columns"][0]["entries"][0]["options"][0],
                         ["Hardens module ingestion, including CVE-2026-14468."])
        self.assertEqual(plan["build"]["notes"], [])

    def test_a_cve_the_public_notes_do_not_name_is_dropped_and_reported(self):
        sections = {"Security": [note("Security vulnerabilities have been addressed and resolved in this update.")]}
        _p, plan = self.plan("Fixes CVE-2026-99999 in the auth path.", sections)
        options = plan["slides"][0]["columns"][0]["entries"][0]["options"]
        self.assertNotIn("CVE-2026-99999", str(options))
        self.assertEqual(options, [["Security"]])
        self.assertIn("disclosure guard", plan["build"]["notes"][0])


class Changelog(unittest.TestCase):
    RULE = {"role": "hcp", "kind": "columns", "source": {"type": "changelog", "product": "hcp-terraform"}}

    def entries(self, items, enrich=None):
        p = planner(slides=[self.RULE], enrich=enrich, changelogs={"products": {"hcp-terraform": {
            "status": "ok", "entries": [{"date": "2026-10-01", "url": "https://example.test/c#2026-10-01",
                                         "items": items}]}}})
        return p.build()["slides"][0]["columns"][0]["entries"]

    def test_several_changes_on_one_day(self):
        entry = self.entries([{"title": "", "text": "SCIM provisioning is now available. It syncs users."},
                              {"title": "", "text": "Audit events are richer."}])[0]
        self.assertEqual(entry["heading"], [{"text": "2026-10-01", "url": "https://example.test/c#2026-10-01"}])
        self.assertEqual(entry["options"][0], ["SCIM provisioning is now available. It syncs users. (+1 more updates)"])
        self.assertEqual(entry["options"][1], ["SCIM provisioning is now available."])

    def test_titled_changes_get_a_bold_lead_in(self):
        entry = self.entries([{"title": "Port 443 access", "text": "Clusters can listen on port 443."}])[0]
        self.assertEqual(entry["options"][0], ["**Port 443 access:** Clusters can listen on port 443."])


class Blogs(unittest.TestCase):
    RULE = {"role": "blogs", "kind": "blogs", "source": {"type": "blogs", "product": "vault"}}
    POSTS = [{"date": "2026-09-03", "title": "Vault agentic IAM is GA", "url": "https://www.hashicorp.com/en/blog/a"},
             {"date": "2026-09-01", "title": "Another post", "url": "https://www.hashicorp.com/blog/b"}]

    def entries(self, enrich):
        p = planner(slides=[self.RULE], blogs={"products": {"vault": {"recent": self.POSTS}}}, enrich=enrich)
        return p.build()["slides"][0]["entries"]

    def test_emphasis_is_reused_only_on_the_exact_title(self):
        enrich = {"blog_titles": {"hashicorp.com/blog/a": {"title_markup": "Vault **agentic IAM** is GA"},
                                  "hashicorp.com/blog/b": {"title_markup": "A **different** title"}}}
        first, second = self.entries(enrich)
        self.assertEqual(first["title"], "Vault **agentic IAM** is GA")
        self.assertEqual(second["title"], "Another post")
        self.assertEqual(first["date_display"], "September 3")


class Events(unittest.TestCase):
    TF = {"role": "tf-events", "kind": "webinars", "source": {"type": "webinars", "product": "terraform"}, "slots": 2}
    VAULT = {"role": "vault-events", "kind": "webinars", "source": {"type": "webinars", "product": "vault"}, "slots": 2}
    FEATURED = {"role": "featured", "kind": "featured", "source": {"type": "featured"}}

    def event(self, title, day, products, kind="WEBINAR", **extra):
        return {"title": title, "date": day, "type": kind, "products": products,
                "url": f"https://example.test/{title.replace(' ', '-')}", **extra}

    def build(self, events):
        return planner(slides=[self.TF, self.VAULT, self.FEATURED], events=events).build()

    def test_soonest_first_shared_events_and_backfill(self):
        both = self.event("Roundtable", "2026-12-08", ["terraform", "vault"], "WORKSHOP")
        plan = self.build({"status": "ok", "featured": [], "webinars": {
            "terraform": [both, self.event("As code", "2026-11-04", ["terraform"]),
                          self.event("Later", "2026-12-20", ["terraform"])],
            "vault": [both], "other": [self.event("Certification", "2026-11-01", [])]}})
        tf, vault = plan["slides"]
        self.assertEqual([s["title"] for s in tf["slots"]], ["As code", "Roundtable"])
        self.assertEqual(tf["later"], ["2026-12-20 Later"])
        self.assertEqual([s["title"] for s in vault["slots"]], ["Certification", "Roundtable"])  # backfilled
        self.assertEqual(vault["slots"][1]["products"], ["terraform", "vault"])
        self.assertEqual(vault["slots"][1]["kind"], "WORKSHOP")
        self.assertEqual(tf["slots"][0]["date_label"], "NOV 4, 2026")
        self.assertEqual(plan["omitted"], [{"role": "featured", "reason": "no upcoming featured events"}])

    def test_description_preference_and_fallback(self):
        url = "https://www.hashicorp.com/en/events/webinars/x"
        events = {"status": "ok", "featured": [], "webinars": {"vault": [], "other": [], "terraform": [
            {"title": "X", "date": "2026-11-04", "type": "WEBINAR", "products": ["terraform"], "url": url,
             "abstract": "The page abstract."},
            {"title": "Y", "date": "2026-11-05", "type": "WORKSHOP", "products": ["terraform"],
             "url": "https://example.test/y", "abstract": ""}]}}
        p = planner(slides=[self.TF], events=events,
                    enrich={"events": {"hashicorp.com/events/webinars/x": {"description": "Curated."}}})
        first, second = p.build()["slides"][0]["slots"]
        self.assertEqual(first["description_options"][:2], ["Curated.", "The page abstract."])
        self.assertEqual(second["description_options"], [plan_deck.GENERIC_EVENT_LINE["WORKSHOP"]])

    def test_unavailable_events_degrade_the_build_instead_of_failing_it(self):
        plan = self.build({"status": "unavailable", "note": "render failed",
                           "webinars": {"terraform": [], "vault": [], "other": []}, "featured": []})
        self.assertEqual(plan["build"]["status"], "degraded")
        self.assertEqual({o["role"] for o in plan["omitted"]}, {"tf-events", "vault-events", "featured"})

    def test_a_partial_source_is_said_in_the_notes_without_degrading_the_build(self):
        events = {"status": "ok", "note": "events page not rendered (render failed: CheckpointError)",
                  "featured": [], "webinars": {"terraform": [self.event("As code", "2026-11-04", ["terraform"])],
                                               "vault": [], "other": []}}
        plan = planner(slides=[self.TF], events=events,
                       blogs={"products": {}, "note": "listing page not read for vault"}).build()
        self.assertEqual(plan["build"]["status"], "ok")
        self.assertEqual(plan["build"]["notes"], ["events: events page not rendered (render failed: CheckpointError)",
                                                  "blogs: listing page not read for vault"])

    def test_featured_event_links_and_text(self):
        events = {"status": "ok", "webinars": {"terraform": [], "vault": [], "other": []}, "featured": [
            {"title": "Exchange Frankfurt", "date": "2026-11-10", "display_date": "November 10",
             "location": "Frankfurt", "url": "https://example.test/a", "products": ["vault"],
             "links": [{"label": "Business Track", "url": "https://example.test/a"},
                       {"label": "Practitioner Track", "url": "https://example.test/b"}],
             "description_points": ["Join **peers**."], "visuals": "template"}]}
        featured = planner(slides=[self.FEATURED], events=events).build()["slides"][0]["events"][0]
        self.assertEqual([link["label"] for link in featured["links"]], ["Business Track", "Practitioner Track"])
        self.assertEqual(featured["description_points"], ["Join **peers**."])
        self.assertEqual(featured["visuals"], "template")


class Cover(unittest.TestCase):
    def test_cover_never_claims_readiness(self):
        cover = planner(slides=[{"role": "cover", "kind": "cover"}]).build()["slides"][0]
        self.assertEqual(cover["month_label"], "October 2026")
        self.assertEqual(cover["status_lines"], ["REVIEW", "Built – 13 October 2026"])
        self.assertEqual((cover["fiscal_year"], cover["week"]), ("FY26", "WEEK 42"))


if __name__ == "__main__":
    unittest.main()
