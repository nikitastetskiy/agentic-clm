"""The rules that stand between a model's output and the deck or the repository."""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401

import check_fix
import enrich
from lib import agent, github, redact, state

KEY = "ica-test-key-0123456789abcdef"   # stands in for the gateway key
SECRET_ENV = {"ICA_API_KEY": KEY, "GIT_TOKEN": "ghs_test-token-9876543210"}


class EnrichmentAnswers(unittest.TestCase):
    NEED = {"id": "terraform-enterprise/2.1.0", "kind": "release", "version": "2.1.0", "date": "2026-10-20",
            "max_bullets": 1, "max_chars": 230,
            "source": {"notes": {"Features": ["Stacks now supports up to 500 deployments per Stack."],
                                 "Security": ["Addresses CVE-2026-14468 in module ingestion."]}}}

    def why(self, *bullets):
        return enrich.problems(self.NEED, list(bullets))

    def test_a_grounded_plain_sentence_is_accepted(self):
        self.assertEqual(self.why("Raises the Stacks limit to 500 deployments and **hardens module ingestion** "
                                  "against CVE-2026-14468."), [])

    def test_facts_that_are_not_in_the_source_are_rejected(self):
        self.assertIn("version 3.4 is not in the source", self.why("Adds compatibility with Terraform 3.4 runs."))
        self.assertIn("CVE-2026-99999 is not in the source",
                      self.why("Fixes CVE-2026-99999 in the authentication path of the platform."))
        self.assertIn("figure 900 is not in the source", self.why("Stacks now supports up to 900 deployments."))

    def test_the_release_s_own_version_may_be_named(self):
        self.assertEqual(self.why("Terraform Enterprise 2.1.0 raises the Stacks limit to 500 deployments."), [])

    def test_shape_and_style_rules(self):
        self.assertTrue(self.why("Too short."))
        self.assertTrue(self.why("x" * 300 + "."))
        self.assertTrue(self.why("Raises the Stacks limit — a big step for platform teams everywhere."))
        self.assertTrue(self.why("See https://example.test for the new Stacks deployment limit details."))
        self.assertTrue(self.why("Raises the Stacks deployment limit and hardens ingestion"))        # no full stop
        self.assertTrue(self.why("A robust release that raises the Stacks deployment limit."))        # banned word
        self.assertTrue(self.why("Raises the **Stacks limit and hardens module ingestion further."))  # odd **
        self.assertEqual(self.why("One sentence that is fine.", "A second one that is not wanted."),
                         ["expected 1-1 bullet(s)"])
        self.assertEqual(enrich.problems(self.NEED, "not a list"), ["expected 1-1 bullet(s)"])


class BlogHistory(unittest.TestCase):
    def test_a_known_post_keeps_its_date_unless_the_listing_page_corrects_it(self):
        known = [{"date": "2026-09-22", "title": "Provider 8.0", "url": "https://www.hashicorp.com/en/blog/p8"}]
        feed = [{"date": "2026-09-23", "title": "Provider 8.0", "url": "https://www.hashicorp.com/blog/p8", "source": "feed"}]
        page = [{**feed[0], "date": "2026-09-21", "source": "product-page"}]
        self.assertEqual(state.merge_blog_posts(known, feed)[0]["date"], "2026-09-22")   # feed = last updated
        self.assertEqual(state.merge_blog_posts(known, page)[0]["date"], "2026-09-21")   # page = published
        self.assertEqual(len(state.merge_blog_posts(known, feed)), 1)                    # /en/ is the same post

    def test_the_same_title_under_a_mistyped_link_is_replaced(self):
        known = [{"date": "2026-07-13", "title": "Preparing for the post-quantum era", "url": "https://x.test/blog/wrong"}]
        fresh = [{"date": "2026-07-13", "title": "Preparing for the Post-Quantum era:", "url": "https://x.test/blog/right",
                  "source": "product-page"}]
        self.assertEqual([p["url"] for p in state.merge_blog_posts(known, fresh)], ["https://x.test/blog/right"])

    def test_newest_first_and_a_recorded_post_keeps_its_place_on_a_tie(self):
        known = [{"date": "2026-07-23", "title": "Stacks, explained", "url": "https://x.test/blog/a"}]
        fresh = [{"date": "2026-07-23", "title": "Another post that day", "url": "https://x.test/blog/b", "source": "feed"},
                 {"date": "2026-09-01", "title": "Newest", "url": "https://x.test/blog/c", "source": "feed"}]
        self.assertEqual([p["title"] for p in state.merge_blog_posts(known, fresh)],
                         ["Newest", "Stacks, explained", "Another post that day"])

    def test_event_keys(self):
        self.assertEqual(state.event_key("https://www.hashicorp.com/en/events/webinars/x/"),
                         state.event_key("http://hashicorp.com/events/webinars/x"))
        self.assertNotEqual(state.event_key("https://ibm.webcasts.com/starthere.jsp?ei=1"),
                            state.event_key("https://ibm.webcasts.com/starthere.jsp?ei=2"))


class FixGuard(unittest.TestCase):
    SUMMARY = "Fixed the TFE parser for the new heading level\n\n## What failed\n..."

    def test_a_small_code_fix_is_allowed(self):
        v = check_fix.judge([("M", "scripts/fetch_tfe_releases.py"), ("??", "tests/fixtures/html/new.html")],
                            42, self.SUMMARY, 600)
        self.assertEqual((v["verdict"], v["reasons"]), ("ok", []))
        self.assertTrue(v["touches_gate"])          # it adds a test fixture: flagged for the reviewer
        self.assertEqual(v["headline"], "Fixed the TFE parser for the new heading level")

    def test_the_pipeline_s_own_plumbing_is_off_limits(self):
        for path in ("ci/self-heal.sh", ".pipeline-config.yaml", ".opencode/agents/repo-fixer.md", "opencode.json",
                     "scripts/check_fix.py", "templates/cadence-template.pptx", "Dockerfile", ".env.example",
                     ".github/workflows/self-heal.yml", ".github/actions/setup/action.yml",
                     "scripts/lib/redact.py", "scripts/scrub_secrets.py"):
            v = check_fix.judge([("M", path)], 3, self.SUMMARY, 600)
            self.assertEqual(v["verdict"], "rejected", path)

    def test_deleting_a_test_or_a_huge_change_is_rejected(self):
        self.assertEqual(check_fix.judge([("D", "tests/test_plan.py")], 300, self.SUMMARY, 600)["verdict"], "rejected")
        self.assertEqual(check_fix.judge([("M", "scripts/plan_deck.py")], 601, self.SUMMARY, 600)["verdict"], "rejected")
        self.assertEqual(check_fix.judge([("M", "scripts/plan_deck.py")], 5, "", 600)["verdict"], "rejected")

    def test_no_fix_and_no_change(self):
        self.assertEqual(check_fix.judge([], 0, "NO-FIX: the events page blocks this network", 600)["verdict"], "no-fix")
        self.assertEqual(check_fix.judge([], 0, self.SUMMARY, 600)["verdict"], "no-change")

    def test_a_change_that_carries_a_secret_is_rejected_without_repeating_it(self):
        leaks = redact.found_in(["+API = '%s'\n" % KEY], SECRET_ENV)
        v = check_fix.judge([("M", "scripts/fetch_blogs.py")], 1, self.SUMMARY, 600, leaks)
        self.assertEqual(v["verdict"], "rejected")
        self.assertEqual(v["reasons"], ["the change contains the value of the secret ICA_API_KEY"])
        self.assertNotIn(KEY, json.dumps(v))


class SecretRedaction(unittest.TestCase):
    def test_every_secret_value_is_replaced_by_its_name(self):
        text = f"key={KEY} token={SECRET_ENV['GIT_TOKEN']} again {KEY}"
        self.assertEqual(redact.scrub(text, SECRET_ENV),
                         "key=[ICA_API_KEY removed] token=[GIT_TOKEN removed] again [ICA_API_KEY removed]")
        self.assertEqual(redact.found_in(["nothing here", text], SECRET_ENV), ["ICA_API_KEY", "GIT_TOKEN"])

    def test_short_placeholders_are_not_treated_as_secrets(self):
        env = {"GIT_TOKEN": "dry-run"}            # the dry run's stand-in token
        self.assertEqual(redact.scrub("a dry-run of the build", env), "a dry-run of the build")
        self.assertEqual(redact.found_in(["dry-run"], env), [])

    def test_files_kept_with_a_run_are_scrubbed_in_place(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "logs").mkdir()
            (root / "logs" / "agent-attempt-1.log").write_text(f"$ echo $ICA_API_KEY\n{KEY}\n", encoding="utf-8")
            (root / "summary.md").write_text("nothing secret\n", encoding="utf-8")
            changed = redact.scrub_tree(root, SECRET_ENV)
            self.assertEqual([p.name for p in changed], ["agent-attempt-1.log"])
            self.assertEqual((root / "logs" / "agent-attempt-1.log").read_text(encoding="utf-8"),
                             "$ echo $ICA_API_KEY\n[ICA_API_KEY removed]\n")

    def test_the_scrub_script_reports_names_never_values(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "fix-summary.md").write_text(f"done {KEY}", encoding="utf-8")
            env = {k: v for k, v in os.environ.items() if k not in redact.SECRET_VARS}
            proc = subprocess.run(["python3", str(helpers.SCRIPTS / "scrub_secrets.py"), tmp],
                                  env={**env, **SECRET_ENV}, capture_output=True, text=True, check=True)
            self.assertIn("scrubbed 1 file(s) of ICA_API_KEY, GIT_TOKEN", proc.stdout)
            self.assertNotIn(KEY, proc.stdout)
            self.assertEqual((Path(tmp) / "fix-summary.md").read_text(encoding="utf-8"), "done [ICA_API_KEY removed]")

    def test_nothing_sent_to_github_carries_a_secret(self):
        sent = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return b"{}"

        def urlopen(req, timeout):
            sent.append(req.data)
            return Response()

        gh = github.GitHub("github.com", "o/r", "t")
        with mock.patch.dict(os.environ, SECRET_ENV), mock.patch.object(github.urllib.request, "urlopen", urlopen):
            gh.open_issue("Build failed", f"agent said: {KEY}", ["build-failure"])
            gh.open_pull("selfheal/x", "main", "self-heal: fix", f"summary\n{KEY}\n")
            with tempfile.TemporaryDirectory() as tmp:
                notes = Path(tmp) / "summary-2026-10-16.md"
                notes.write_text(f"notes {KEY}", encoding="utf-8")
                gh.upload_asset({"id": 1, "assets": [], "upload_url": "https://uploads.invalid/a{?name}"}, notes)
        self.assertEqual(len(sent), 4)                  # issue, pull lookup, pull, asset
        for body in sent:
            self.assertNotIn(KEY.encode(), body or b"")
        self.assertIn(b"[ICA_API_KEY removed]", sent[0])
        self.assertIn(b"[ICA_API_KEY removed]", sent[3])

    def test_the_agent_transcript_is_scrubbed(self):
        done = subprocess.CompletedProcess([], 0, stdout=f"I read the key: {KEY}\n")
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, SECRET_ENV), \
                mock.patch.object(agent.subprocess, "run", return_value=done):
            log_file = Path(tmp) / "agent-attempt-1.log"
            agent.run("repo-fixer", "fix it", model="m", cwd=Path(tmp), log_file=log_file)
            self.assertEqual(log_file.read_text(encoding="utf-8"), "I read the key: [ICA_API_KEY removed]\n")


class GitRemotes(unittest.TestCase):
    def test_ssh_and_https_remotes(self):
        for url in ("git@github.ibm.com:Some-Owner/auto-pptx-csm.git",
                    "https://github.ibm.com/Some-Owner/auto-pptx-csm.git",
                    "https://x-access-token@github.ibm.com/Some-Owner/auto-pptx-csm",
                    "ssh://git@github.ibm.com/Some-Owner/auto-pptx-csm.git"):
            self.assertEqual(github.REMOTE_RE.match(url).groups(), ("github.ibm.com", "Some-Owner/auto-pptx-csm"), url)

    def test_api_base(self):
        self.assertEqual(github.GitHub("github.com", "o/r", "t").api, "https://api.github.com")
        self.assertEqual(github.GitHub("github.ibm.com", "o/r", "t").api, "https://github.ibm.com/api/v3")

    def test_publishing_a_date_again_removes_files_the_new_build_did_not_make(self):
        gh = github.GitHub("github.invalid", "o/r", "t", dry_run=True)
        release = {"assets": [{"id": 1, "name": "deck-2026-10-16.pptx"}, {"id": 2, "name": "deck-2026-10-16.pdf"}]}
        self.assertEqual(gh.prune_assets(release, {"deck-2026-10-16.pptx", "summary.md"}), ["deck-2026-10-16.pdf"])
        self.assertEqual(gh.calls, ["DELETE https://github.invalid/api/v3/repos/o/r/releases/assets/2"])

    def test_a_dry_run_makes_no_write_call(self):
        gh = github.GitHub("github.invalid", "o/r", "t", dry_run=True)
        gh.request("POST", "/releases", {"tag_name": "deck-x"})
        gh.request("DELETE", "/releases/1")
        self.assertEqual(gh.calls, ["POST https://github.invalid/api/v3/repos/o/r/releases",
                                    "DELETE https://github.invalid/api/v3/repos/o/r/releases/1"])


if __name__ == "__main__":
    unittest.main()
