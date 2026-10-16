"""The fetchers' parsers, against small saved samples of each page structure."""
import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401  (puts scripts/ on the import path)

import fetch_blogs
import fetch_changelogs
import fetch_events
import fetch_hcp_changelogs
import fetch_release_notes
import fetch_releases
import fetch_tfe_releases
from lib import browser, common


class VaultReleaseNotes(unittest.TestCase):
    def setUp(self):
        self.notes = fetch_release_notes.parse_notes_page(helpers.html("vault-release-notes.html"), "vault")

    def test_only_version_headings_are_releases(self):
        self.assertEqual(sorted(self.notes), ["2.1.1", "2.1.2"])  # "Vault 9.9.9" in prose is not one

    def test_release_date_and_anchor(self):
        self.assertEqual(self.notes["2.1.2"]["released"], "2026-10-07")
        self.assertEqual(self.notes["2.1.2"]["anchor"], "vault-2-1-2")

    def test_empty_sections_are_dropped(self):
        # "New features: None." and "Improvements: None." must not count as content
        self.assertEqual(list(self.notes["2.1.2"]["sections"]), ["Bug fixes and security patches"])

    def test_lead_in_and_enterprise_badge(self):
        first, second = self.notes["2.1.2"]["sections"]["Bug fixes and security patches"]
        self.assertEqual((first["lead"], first["enterprise"]), ("Fixed", False))
        self.assertEqual(second["lead"], "Secrets sync")
        self.assertTrue(second["enterprise"])
        self.assertEqual(second["text"], "Fixed the total secrets count on the overview page.")

    def test_badge_is_not_part_of_a_section_name(self):
        self.assertEqual(list(self.notes["2.1.1"]["sections"]), ["New features"])


class TerraformEnterpriseSeries(unittest.TestCase):
    def setUp(self):
        self.page = helpers.html("tfe-series.html")

    def test_versions_and_dates(self):
        rows = fetch_tfe_releases.parse_series_page(self.page, "2.0")
        self.assertEqual([(r["version"], r["date"]) for r in rows],
                         [("2.0.8", "2026-09-23"), ("2.0.7", "2026-09-18"), ("2.0.2", "2026-05-15")])
        self.assertEqual(rows[0]["release_notes_url"],
                         "https://developer.hashicorp.com/terraform/enterprise/releases/2.0.x#2-0-8")

    def test_sections_stop_at_the_next_release_whatever_its_heading_level(self):
        notes = fetch_tfe_releases.parse_series_sections(self.page)
        self.assertEqual(list(notes["2.0.8"]), ["Bug Fixes"])
        self.assertEqual(list(notes["2.0.7"]), ["Features", "Security"])   # not 2.0.2's fixes
        self.assertEqual(list(notes["2.0.2"]), ["Bug Fixes"])              # an <h2> entry among <h1>s

    def test_a_version_in_a_table_is_not_a_release(self):
        self.assertNotIn("14.4.0", fetch_tfe_releases.parse_series_sections(self.page))


class HcpChangelogs(unittest.TestCase):
    def test_terraform_entries_within_the_window(self):
        entries = fetch_hcp_changelogs.parse_changelog(
            helpers.html("hcp-terraform-changelog.html"), "https://example.test/changelog", "2026-05-01")
        self.assertEqual([e["date"] for e in entries], ["2026-10-02", "2026-10-01"])
        self.assertEqual(entries[0]["url"], "https://example.test/changelog#2026-10-02")
        self.assertEqual(len(entries[1]["items"]), 2)
        self.assertEqual(entries[1]["items"][1]["text"],
                         "The create_token audit event now includes richer metadata.")

    def test_vault_entries_carry_their_title(self):
        entries = fetch_hcp_changelogs.parse_changelog(
            helpers.html("hcp-vault-changelog.html"), "https://example.test/vault", "2026-05-01")
        self.assertEqual(entries[0]["items"][0]["title"], "Port 443 access")
        self.assertTrue(entries[0]["items"][0]["text"].startswith("HCP Vault Dedicated clusters can now"))
        self.assertEqual(entries[0]["month_label"], "October")


class TerraformChangelog(unittest.TestCase):
    TEXT = ("## 1.16.6 (Unreleased)\n\n## 1.16.5 (September 30, 2026)\n\n\nBUG FIXES:\n\n"
            "* Fixed a crash on tainted state ([#39287](https://github.com/hashicorp/terraform/issues/39287))\n\n"
            "## 1.16.0 (August 26, 2026)\n\nNEW FEATURES:\n\n* A feature\n\nBUG FIXES:\n\n* A fix\n")

    def test_release_date_comes_from_the_heading(self):
        self.assertEqual(fetch_changelogs.heading_date(self.TEXT, "1.16.5"), "2026-09-30")
        self.assertIsNone(fetch_changelogs.heading_date(self.TEXT, "1.16.6"))   # unreleased

    def test_sections_are_grouped_and_cleaned(self):
        groups = fetch_changelogs.grouped_sections(fetch_changelogs.section_for(self.TEXT, "1.16.5"))
        self.assertEqual(groups, {"BUG FIXES": [{"lead": "", "text": "Fixed a crash on tainted state",
                                                 "enterprise": False}]})
        self.assertEqual(list(fetch_changelogs.grouped_sections(
            fetch_changelogs.section_for(self.TEXT, "1.16.0"))), ["NEW FEATURES", "BUG FIXES"])


class ReleasesApi(unittest.TestCase):
    RAW = [
        {"version": "2.1.2+ent.hsm", "timestamp_created": "2026-10-07T10:00:02Z", "is_prerelease": False},
        {"version": "2.1.2+ent", "timestamp_created": "2026-10-07T10:00:01Z", "is_prerelease": False},
        {"version": "2.1.2", "timestamp_created": "2026-10-07T10:00:00Z", "is_prerelease": False},
        {"version": "1.21.12+ent", "timestamp_created": "2026-10-07T09:00:00Z", "is_prerelease": False},
        {"version": "2.2.0-rc1", "timestamp_created": "2026-10-08T09:00:00Z", "is_prerelease": True},
    ]

    def entries(self):
        return fetch_releases.to_entries(self.RAW, normalize=True, changelog_url_template=None,
                                         release_notes_url_template=None,
                                         link_template="https://example.test/v{version}")

    def test_build_variants_fold_into_one_entry(self):
        by_version = {e["version"]: e for e in self.entries()}
        self.assertEqual(sorted(by_version), ["1.21.12", "2.1.2", "2.2.0-rc1"])
        self.assertEqual(by_version["2.1.2"]["variants"], ["", "ent", "ent.hsm"])

    def test_enterprise_only_lines_and_prereleases_are_flagged(self):
        by_version = {e["version"]: e for e in self.entries()}
        self.assertTrue(by_version["1.21.12"]["enterprise_only"])
        self.assertFalse(by_version["2.1.2"]["enterprise_only"])
        self.assertTrue(by_version["2.2.0-rc1"]["is_prerelease"])
        self.assertEqual(by_version["1.21.12"]["line"], "1.21")
        self.assertEqual(by_version["2.1.2"]["url"], "https://example.test/v2.1.2")


class Blogs(unittest.TestCase):
    TERRAFORM = {"names": ["terraform"], "keywords": ["terraform", "infrastructure as code"]}
    VAULT = {"names": ["vault"], "keywords": ["vault", "secrets management"]}

    def post(self, title, listed_on=(), summary=""):
        return {"title": title, "url": "https://www.hashicorp.com/blog/" + title.lower().replace(" ", "-"),
                "listed_on": list(listed_on), "summary": summary}

    def test_a_title_naming_the_product_belongs_to_it(self):
        self.assertTrue(fetch_blogs.belongs(self.post("Terraform 1.16 is here"), "terraform", self.TERRAFORM))

    def test_a_title_naming_another_product_never_does(self):
        # listed on the Terraform page, and "sentinel"-style keyword bait in the summary
        post = self.post("Stream HCP Vault Dedicated audit logs to Microsoft Sentinel",
                         listed_on=["terraform"], summary="terraform infrastructure as code")
        self.assertFalse(fetch_blogs.belongs(post, "terraform", self.TERRAFORM))
        self.assertTrue(fetch_blogs.belongs(post, "vault", self.VAULT))
        self.assertFalse(fetch_blogs.belongs(self.post("Secure AI agents with HashiCorp Boundary",
                                                       summary="secrets management"), "vault", self.VAULT))

    def test_a_title_naming_no_product_follows_its_listing_page_then_keywords(self):
        self.assertTrue(fetch_blogs.belongs(self.post("Preparing for the post-quantum era", ["vault"]),
                                            "vault", self.VAULT))
        self.assertTrue(fetch_blogs.belongs(self.post("Why policy matters", summary="secrets management today"),
                                            "vault", self.VAULT))
        self.assertFalse(fetch_blogs.belongs(self.post("Why policy matters"), "vault", self.VAULT))

    def test_listing_cards(self):
        cards = [{"href": "/blog/terraform-116", "text": "TECHNOLOGY & SOLUTIONS\nTerraform 1.16\nSep 28, 2026 |\n5 min read"},
                 {"href": "/blog/terraform-116", "text": "Terraform 1.16\nSep 28, 2026 |"},   # same post twice
                 {"href": "/blog/products/terraform", "text": "All posts"},                   # not a post
                 {"href": "/en/blog/no-date", "text": "A card without a date"}]
        posts = fetch_blogs.parse_listing(cards, "terraform")
        self.assertEqual([(p["title"], p["date"], p["url"]) for p in posts],
                         [("Terraform 1.16", "2026-09-28", "https://www.hashicorp.com/en/blog/terraform-116")])


class Events(unittest.TestCase):
    CARDS = [
        {"href": "/events/webinars/a", "text": "NOV 4, 2026|\nWEBINAR\nBuild everything as code\nTerraform", "products": ["terraform"]},
        {"href": "https://events.example.test/x", "text": "DEC 8, 2026|\nWORKSHOP\nExecutive roundtable\nVault\nTerraform", "products": ["vault", "terraform"]},
        {"href": "/events/summit", "text": "NOV 20, 2026|\nCONFERENCE\nCloud Summit - London", "products": []},
        {"href": "/events/old", "text": "JAN 5, 2026|\nWEBINAR\nLong gone\nVault", "products": ["vault"]},
    ]
    CFG = {"terraform": {}, "vault": {}}

    def test_slot_types_fill_the_product_slides_and_the_rest_is_featured(self):
        events = fetch_events.build_events(self.CARDS, "2026-10-13", self.CFG, ("WEBINAR", "WORKSHOP"))
        self.assertEqual([e["title"] for e in events["webinars"]["terraform"]],
                         ["Build everything as code", "Executive roundtable"])
        self.assertEqual([(e["title"], e["type"]) for e in events["webinars"]["vault"]],
                         [("Executive roundtable", "WORKSHOP")])           # badged for both products
        self.assertEqual([(e["title"], e["location"]) for e in events["featured"]],
                         [("Cloud Summit - London", "London")])

    def test_past_events_are_dropped(self):
        events = fetch_events.build_events(self.CARDS, "2026-10-13", self.CFG, ("WEBINAR",))
        self.assertNotIn("Long gone", [e["title"] for b in events["webinars"].values() for e in b])
        # without WORKSHOP as a slot type the roundtable is an event of its own
        self.assertIn("Executive roundtable", [e["title"] for e in events["featured"]])

    def test_abstract_skips_boilerplate(self):
        blocks = ["Register", "By submitting this form, I acknowledge that I have read and understand the Privacy "
                  "Statement of the company and agree to it.",
                  "Automation has grown from simple scripts on servers to the deployment and management of entire "
                  "environments. In this webinar learn how to manage everything as code.",
                  "**Build Consistent Infrastructure Delivery** A marketing tile that is not part of the abstract text."]
        self.assertTrue(fetch_events.pick_abstract(blocks).startswith("Automation has grown"))
        self.assertNotIn("Privacy", fetch_events.pick_abstract(blocks))
        self.assertEqual(fetch_events.pick_abstract(["too short"]), "")

    def test_a_hand_added_webinar_becomes_a_card_and_an_event_a_slide(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "featured-events.yaml"
            path.write_text(
                "events:\n"
                "  - {title: Zero Trust for AI, type: Webinar, date: 2026-11-17, products: [vault],\n"
                "     links: [{label: Register, url: 'https://ibm.webcasts.example/zt'}],\n"
                "     description_points: [Why AI agents are a new class of identity.]}\n"
                "  - {title: Exchange Frankfurt, date: 2026-11-10, location: Frankfurt, products: [vault],\n"
                "     links: [{label: Business Track, url: 'https://example.test/fra'}]}\n"
                "  - {title: Long gone, type: Webinar, date: 2026-01-01, products: [vault],\n"
                "     links: [{label: Register, url: 'https://example.test/old'}]}\n", encoding="utf-8")
            manual = fetch_events.load_manual_events(path, "2026-10-13")
        events = fetch_events.build_events(self.CARDS, "2026-10-13", self.CFG, ("WEBINAR", "WORKSHOP"))
        fetch_events.merge_manual(events, manual, ("WEBINAR", "WORKSHOP"))
        vault = events["webinars"]["vault"]
        self.assertEqual([(e["title"], e["type"], e["display_date"]) for e in vault],
                         [("Zero Trust for AI", "WEBINAR", "Nov 17, 2026"), ("Executive roundtable", "WORKSHOP", "Dec 8, 2026")])
        self.assertEqual(vault[0]["description"], "Why AI agents are a new class of identity.")
        self.assertEqual(events["webinars"]["terraform"][0]["title"], "Build everything as code")   # vault only
        self.assertEqual([e["title"] for e in events["featured"]], ["Exchange Frankfurt", "Cloud Summit - London"])

    # The "Featured Events" box of the events page (2026-10-14): promotions without a date.
    PROMOS = [
        {"href": "/en/lp/agentic-runtime-security-exchange", "cta": "Register",
         "text": "EXECUTIVE EVENT SERIES\nAgentic Runtime Security Exchange Series\nDesigned to move beyond theory.\nVault\nRegister"},
        {"href": "https://ibm.webcasts.com/starthere.jsp?ei=1", "cta": "Register",
         "text": "WEBINAR\nModernizing Your Zero Trust Program for AI\nIn this session, we explore zero trust.\nVault\nRegister"},
        {"href": "/en/lp/cloud-fundamentals", "cta": "Watch now",
         "text": "WEBINAR SERIES\nCloud Fundamentals\nDiscover how to enable self-service.\nTerraform\nWatch now"},
        {"href": "/events/webinars/recorded", "cta": "Watch now", "text": "On-demand webinars\nLearn best practices\nWatch now"},
    ]
    SERIES_PAGE = ["Attend an Agentic Runtime Security Exchange near you", "Frankfurt", "10 November", "Register here",
                   "New York City", "Coming soon", "Riyadh", "Coming soon", "Past Events",
                   "London", "25 June", "Registration closed"]

    def test_undated_promotions_are_read_and_recordings_skipped(self):
        promos = [p for p in (fetch_events.parse_promo(c) for c in self.PROMOS) if p]
        self.assertEqual([(p["type"], p["title"], p["products"]) for p in promos],
                         [("EXECUTIVE EVENT SERIES", "Agentic Runtime Security Exchange Series", ["vault"]),
                          ("WEBINAR", "Modernizing Your Zero Trust Program for AI", ["vault"])])
        self.assertEqual(promos[0]["url"], "https://www.hashicorp.com/en/lp/agentic-runtime-security-exchange")

    def test_series_sessions_stop_at_past_events(self):
        self.assertEqual(fetch_events.parse_series_sessions(self.SERIES_PAGE),
                         [{"place": "Frankfurt", "when": "10 November"},
                          {"place": "New York City", "when": "Coming soon"},
                          {"place": "Riyadh", "when": "Coming soon"}])

    def test_unscheduled_events_skip_what_is_shown_and_what_is_out_of_scope(self):
        events = fetch_events.build_events(self.CARDS, "2026-10-13", self.CFG, ("WEBINAR", "WORKSHOP"))
        events["featured"].append({"title": "Agentic Runtime Security Exchange Frankfurt", "location": "Frankfurt"})
        promos = [p for p in (fetch_events.parse_promo(c) for c in self.PROMOS) if p]
        out = fetch_events.unscheduled_events(promos, events, ["EMEA", "MENA"],
                                              lambda url: fetch_events.parse_series_sessions(self.SERIES_PAGE))
        self.assertEqual([(u["title"], u["when"], u["format"]) for u in out],
                         [("Agentic Runtime Security Exchange Series — Riyadh", "Coming soon", "in person"),  # not NYC
                          ("Modernizing Your Zero Trust Program for AI", "no date on the events page", "online")])

    def test_the_region_scope_applies_to_in_person_events_only(self):
        """Webinars and workshops are online: a place in the title, or the page's "Americas" tag (the
        audience time zone), never drops one. An in-person event outside the scope is dropped."""
        cards = self.CARDS + [
            {"href": "/events/webinars/nyc", "text": "NOV 5, 2026|\nWEBINAR\nLessons from New York banks\nVault",
             "products": ["vault"]},
            {"href": "/events/toronto", "text": "NOV 6, 2026|\nCONFERENCE\nCloud Summit - Toronto", "products": []}]
        events = fetch_events.build_events(cards, "2026-10-13", self.CFG, ("WEBINAR", "WORKSHOP"))
        report = fetch_events.scope_in_person(events, ["EMEA", "MENA"])
        self.assertIn("Lessons from New York banks", [e["title"] for e in events["webinars"]["vault"]])
        self.assertEqual([e["title"] for e in events["featured"]], ["Cloud Summit - London"])
        self.assertEqual([d["title"] for d in report["dropped"]], ["Cloud Summit - Toronto"])



class EventsIndex(unittest.TestCase):
    """The events page's search index (tests/fixtures/events-index.json: its real records on 2026-10-16
    plus hand-made ones for the cases the parser must skip or link differently)."""
    TODAY = "2026-10-16"
    CFG = {"terraform": {}, "vault": {}}

    def setUp(self):
        self.hits = json.loads((helpers.FIXTURES / "events-index.json").read_text(encoding="utf-8"))["results"][0]["hits"]

    def parsed(self):
        return [e for e in (fetch_events.parse_hit(h, self.TODAY) for h in self.hits) if e]

    def test_records_become_the_events_the_page_shows(self):
        by_title = {e["title"]: e for e in self.parsed()}
        webinar = by_title["Build, deploy, and manage everything as code"]   # the index has a trailing space
        self.assertEqual((webinar["date"], webinar["display_date"], webinar["type"], webinar["products"]),
                         ("2026-11-04", "Nov 4, 2026", "WEBINAR", ["terraform"]))
        self.assertEqual(webinar["url"],
                         "https://www.hashicorp.com/events/webinars/build-deploy-and-manage-everything-as-code")
        roundtable = by_title["Executive roundtable: Agentic Runtime Security"]
        self.assertEqual((roundtable["type"], roundtable["products"], roundtable["url"]),
                         ("WORKSHOP", ["terraform", "vault"], "https://events.hashicorp.com/AgenticRuntimeSecurity"))

    def test_hidden_recorded_past_and_undated_records_are_left_out(self):
        titles = {e["title"] for e in self.parsed()}
        self.assertEqual(len(titles), 6)
        self.assertFalse(titles & {"A hidden webinar", "A recorded webinar", "Long gone", "No usable date"})

    def test_the_index_fills_the_same_slides_as_the_page(self):
        events = fetch_events.bucket_events(self.parsed(), self.TODAY, self.CFG, ("WEBINAR", "WORKSHOP"))
        self.assertEqual([e["date"] for e in events["webinars"]["terraform"]],
                         ["2026-11-04", "2026-11-12", "2026-12-08", "2026-12-09"])
        self.assertEqual([e["date"] for e in events["webinars"]["vault"]], ["2026-12-08", "2026-12-10"])
        self.assertEqual([(e["title"], e["location"]) for e in events["featured"]],
                         [("Cloud Summit - London", "London")])

    def test_the_page_fills_a_missing_link_and_keeps_what_only_it_shows(self):
        indexed = self.parsed()
        self.assertEqual(next(e["url"] for e in indexed if e["title"] == "Cloud Summit - London"),
                         fetch_events.EVENTS_URL)                     # the record has no link of its own
        rendered = fetch_events.parse_cards([
            {"href": "/events/cloud-summit-london", "text": "NOV 20, 2026|\nCONFERENCE\nCloud Summit - London",
             "products": []},
            {"href": "/events/webinars/late", "text": "DEC 15, 2026|\nWEBINAR\nAdded since the index\nVault",
             "products": ["vault"]}], self.TODAY)
        listed, differ = fetch_events.reconcile(indexed, rendered)
        by_title = {e["title"]: e for e in listed}
        self.assertEqual(len(listed), 7)
        self.assertEqual(by_title["Cloud Summit - London"]["url"], "https://www.hashicorp.com/events/cloud-summit-london")
        self.assertIn("Added since the index", by_title)
        self.assertIn("only on the page: 2026-12-15 added since the index", differ)
        self.assertEqual(sum(d.startswith("only in the index") for d in differ), 5)
        self.assertEqual(fetch_events.reconcile(None, rendered), (rendered, []))

    def test_registration_pages_elsewhere_are_not_opened_by_the_own_site_browser(self):
        events = fetch_events.bucket_events(self.parsed(), self.TODAY, self.CFG, ("WEBINAR", "WORKSHOP"))
        own = fetch_events.event_pages(events, browser.own_site)
        elsewhere = fetch_events.event_pages(events, lambda url: not browser.own_site(url))
        self.assertTrue(all(url.startswith("https://www.hashicorp.com/") for url in own))
        self.assertEqual(sorted(elsewhere), ["https://events.hashicorp.com/AgenticRuntimeSecurity",
                                             "https://events.hashicorp.com/TerraformDec",
                                             "https://events.hashicorp.com/VaultQuickstartDec"])
        self.assertEqual(len(elsewhere["https://events.hashicorp.com/AgenticRuntimeSecurity"]), 2)  # both slides

    def test_without_the_page_the_dated_events_still_come_from_the_index(self):
        @contextmanager
        def blocked():
            raise browser.CheckpointError("security checkpoint did not clear for https://www.hashicorp.com/en/events")
            yield  # noqa: unreachable — makes this a generator, as session() is

        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(common, "DATA_DIR", Path(tmp)), \
                mock.patch.object(fetch_events, "index_hits", return_value=self.hits), \
                mock.patch.object(fetch_events.browser, "session", blocked), \
                mock.patch.object(fetch_events, "run_date", return_value=date(2026, 10, 16)):
            self.assertEqual(fetch_events.main(), 0)
            events = json.loads((Path(tmp) / "events.json").read_text(encoding="utf-8"))
        self.assertEqual((events["status"], events["dated_from"]), ("ok", "index"))
        self.assertEqual((events["counts"]["terraform_webinars"], events["counts"]["vault_webinars"]), (4, 2))
        self.assertIn("events page not rendered (render failed: CheckpointError)", events["note"])


class BlogListings(unittest.TestCase):
    def test_an_unread_listing_page_is_said(self):
        pages = {"terraform": "https://example.test/t", "vault": "https://example.test/v"}
        self.assertEqual(fetch_blogs.listing_note(pages, {"terraform": [], "vault": []}), "")
        self.assertTrue(fetch_blogs.listing_note(pages, {"terraform": []}).startswith("listing page not read for vault;"))


class FetcherOutput(unittest.TestCase):
    def test_data_can_live_outside_the_repository(self):
        # A scratch run points AUTO_PPTX_DATA_DIR outside the checkout; writing there must not fail.
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(common, "DATA_DIR", Path(tmp)):
            path = common.write_output("blogs.json", {"products": {}})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["products"], {})


if __name__ == "__main__":
    unittest.main()
