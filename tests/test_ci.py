"""The pipelines on GitHub Actions: the workflow files, the hand-over from ci/ to the self-heal job, and
the preflight's check of the gateway key.

These are the mistakes that would otherwise only show on GitHub, after a failed Monday run: a self-heal
job that waits for an output nobody writes, a token left behind for the repair agent to find, a
setting the scripts never read, a key the gateway no longer accepts.
"""
import contextlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import yaml

import helpers
import review_pr
from lib import gateway

GITHUB = helpers.REPO / ".github"
WEEKLY = "weekly-deck.yml"
SELF_HEAL = "self-heal.yml"
REVIEW = "pr-review.yml"
# Set by the workflow for the scripts, but not pipeline properties.
SECRET_EXPR = r"\$\{\{\s*secrets\."
RUNNER_ENV = {"CI_DRY_RUN", "PIPELINE_RUN_URL", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL"}


def workflow(name: str) -> dict:
    return yaml.safe_load((GITHUB / "workflows" / name).read_text(encoding="utf-8"))


def triggers(wf: dict) -> dict:
    return wf.get("on") or wf.get(True)   # YAML 1.1 reads a bare `on:` key as true


def steps(wf: dict):
    for job in wf["jobs"].values():
        yield from job.get("steps", [])


def pipeline_step(wf: dict) -> dict:
    """The step that runs ci/run.sh."""
    [step] = [s for s in steps(wf) if "bash ci/run.sh" in s.get("run", "")]
    return step


def ci_properties() -> set[str]:
    """Every setting ci/ reads, as the environment variable it is read from (`prop event-scope` is
    EVENT_SCOPE)."""
    text = "\n".join(p.read_text(encoding="utf-8") for p in (helpers.REPO / "ci").glob("*.sh"))
    return {n.upper().replace("-", "_") for n in re.findall(r"\b(?:prop|secret) ([a-z][a-z-]+)", text)}


def ci_defaults() -> dict[str, str]:
    """What each setting ci/ reads falls back to when it is not set (`prop strict 0` is 0)."""
    text = "\n".join(p.read_text(encoding="utf-8") for p in (helpers.REPO / "ci").glob("*.sh"))
    return dict(re.findall(r"\bprop ([a-z][a-z-]+) ([^\s\"()]+)\)", text))


def trigger_selfheal(**env) -> tuple[int, str]:
    """Run ci_trigger_selfheal as a failed weekly build does; (its exit code, what it wrote to the
    step's GITHUB_OUTPUT file). The test's own runner environment is left out."""
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "github-output"
        out.write_text("", encoding="utf-8")
        base = {k: v for k, v in os.environ.items()
                if k not in ("GITHUB_ACTIONS", "CI_DRY_RUN", "SELFHEAL_WEBHOOK_URL")}
        proc = subprocess.run(["bash", "-c", "source ci/lib.sh && ci_trigger_selfheal fetch-blogs 10"],
                              cwd=helpers.REPO, env={**base, "GITHUB_OUTPUT": str(out), **env},
                              capture_output=True, text=True)
        return proc.returncode, out.read_text(encoding="utf-8")


class SelfHealHandOver(unittest.TestCase):
    def test_on_github_actions_a_failed_build_asks_for_the_self_heal_job(self):
        code, output = trigger_selfheal(GITHUB_ACTIONS="true")
        self.assertEqual(code, 0)
        self.assertEqual(output.splitlines(), ["self-heal=true", "failed-step=fetch-blogs", "failed-code=10"])

    def test_a_rehearsal_asks_for_nothing(self):
        self.assertEqual(trigger_selfheal(GITHUB_ACTIONS="true", CI_DRY_RUN="1"), (0, ""))

    def test_elsewhere_without_a_webhook_nothing_is_started(self):
        self.assertEqual(trigger_selfheal(), (1, ""))

    def test_the_self_heal_job_waits_for_that_output(self):
        weekly = workflow(WEEKLY)
        build, heal = weekly["jobs"]["build"], weekly["jobs"]["self-heal"]
        self.assertEqual(pipeline_step(weekly)["id"], "run")
        self.assertEqual(build["outputs"]["self-heal"], "${{ steps.run.outputs.self-heal }}")
        self.assertIn("needs.build.outputs.self-heal == 'true'", heal["if"])
        self.assertEqual(heal["uses"], f"./.github/workflows/{SELF_HEAL}")
        self.assertEqual(set(triggers(workflow(SELF_HEAL))), {"workflow_call", "workflow_dispatch"})

    def test_the_self_heal_job_is_granted_what_the_repair_needs(self):
        # A called workflow's jobs get at most what the calling job grants.
        rank = {"none": 0, "read": 1, "write": 2}
        granted = workflow(WEEKLY)["jobs"]["self-heal"]["permissions"]
        for name, job in workflow(SELF_HEAL)["jobs"].items():
            for scope, level in job["permissions"].items():
                self.assertGreaterEqual(rank[granted.get(scope, "none")], rank[level], f"{name}: {scope}")


class Workflows(unittest.TestCase):
    def test_both_run_the_pipeline_entry_point_in_their_mode(self):
        self.assertEqual(pipeline_step(workflow(WEEKLY))["env"]["PIPELINE_MODE"], "${{ inputs.mode || 'weekly' }}")
        self.assertEqual(pipeline_step(workflow(SELF_HEAL))["env"]["PIPELINE_MODE"], "self-heal")

    def test_every_setting_passed_in_is_one_ci_reads(self):
        known = ci_properties() | RUNNER_ENV
        for name in (WEEKLY, SELF_HEAL):
            unknown = set(pipeline_step(workflow(name))["env"]) - known
            self.assertEqual(unknown, set(), name)

    def test_no_checkout_leaves_a_token_behind(self):
        for name in (WEEKLY, SELF_HEAL, REVIEW):
            for step in steps(workflow(name)):
                if step.get("uses", "").startswith("actions/checkout@"):
                    self.assertIs(step.get("with", {}).get("persist-credentials"), False, name)

    def test_secrets_reach_only_the_repository_s_own_scripts(self):
        own = ("bash ci/run.sh", "python3 scripts/scrub_secrets.py output data", "python3 scripts/review_pr.py")
        for name in (WEEKLY, SELF_HEAL, REVIEW):
            wf = workflow(name)
            self.assertEqual(wf["permissions"], {"contents": "read"}, name)
            self.assertNotRegex(str(wf.get("env", {})), SECRET_EXPR, name)
            for job in wf["jobs"].values():
                self.assertNotRegex(str(job.get("env", {})), SECRET_EXPR, name)
            for step in steps(wf):
                self.assertNotRegex(step.get("run", ""), SECRET_EXPR, name)   # never pasted into a script
                self.assertNotRegex(str(step.get("with", {})), SECRET_EXPR, name)
                if not any(cmd in step.get("run", "") for cmd in own):
                    self.assertNotRegex(str(step.get("env", {})), SECRET_EXPR, f"{name}: {step.get('name') or step.get('uses')}")

    def test_saved_files_are_scrubbed_with_every_secret_the_run_had(self):
        for name in (WEEKLY, SELF_HEAL):
            wf = workflow(name)
            run_steps = list(steps(wf))
            [scrub] = [s for s in run_steps if "scripts/scrub_secrets.py" in s.get("run", "")]
            [save] = [s for s in run_steps if s.get("uses", "").startswith("actions/upload-artifact@")]
            self.assertLess(run_steps.index(scrub), run_steps.index(save), name)
            self.assertEqual(scrub["if"], "always()", name)
            given = {k for k, v in pipeline_step(wf)["env"].items() if re.search(SECRET_EXPR, v)}
            self.assertEqual(set(scrub["env"]), given, name)

    def test_the_key_reaches_only_runs_from_main_where_an_environment_holds_it(self):
        # With KEY_ENVIRONMENT set, the key is an environment secret only main may use: a workflow
        # changed on another branch, which needs no review, cannot get at it (docs/pipelines.md).
        main_only = "${{ github.ref == 'refs/heads/main' && vars.KEY_ENVIRONMENT || '' }}"
        for name in (WEEKLY, SELF_HEAL, REVIEW):
            for job_name, job in workflow(name)["jobs"].items():
                given = str([s.get("env", {}) for s in job.get("steps", [])])
                if "secrets.ICA_API_KEY" in given or "secrets.BOB_API_KEY" in given:
                    self.assertEqual(job.get("environment"), main_only, f"{name}: {job_name}")

    def test_every_action_from_elsewhere_is_pinned_to_a_commit(self):
        # The hashicorp org checks this (custom property GHA_Pinned_Hashes_Check): a tag can be moved to
        # other code, a commit cannot.
        for path in [*(GITHUB / "workflows").glob("*.yml"), *(GITHUB / "actions").glob("*/action.yml")]:
            for ref in re.findall(r"(?m)^[\s-]*uses:\s*(\S+)", path.read_text(encoding="utf-8")):
                if not ref.startswith("./"):
                    self.assertRegex(ref, r"@[0-9a-f]{40}$", path.name)

    def test_the_runner_installs_the_image_s_opencode_version(self):
        action = (GITHUB / "actions" / "setup" / "action.yml").read_text(encoding="utf-8")
        self.assertIn("s/^ARG OPENCODE_VERSION=//p' Dockerfile", action)
        self.assertRegex((helpers.REPO / "Dockerfile").read_text(encoding="utf-8"),
                         r"(?m)^ARG OPENCODE_VERSION=\d+\.\d+\.\d+$")


class ReviewWorkflow(unittest.TestCase):
    """.github/workflows/pr-review.yml runs main's code with the keys, for a pull request it only reads."""

    def test_nothing_from_the_pull_request_is_checked_out_or_run(self):
        wf = workflow(REVIEW)
        self.assertEqual(list(triggers(wf)), ["pull_request_target"])   # this file and the scripts as on main
        for step in steps(wf):
            if step.get("uses", "").startswith("actions/checkout@"):
                self.assertNotIn("ref", step.get("with", {}))            # the base branch, never the head
            self.assertNotIn("github.event.pull_request", step.get("run", ""))
        self.assertEqual(pipeline_step_of(wf, "scripts/review_pr.py")["run"], "python3 scripts/review_pr.py")

    def test_it_may_read_and_comment_nothing_more(self):
        wf = workflow(REVIEW)
        self.assertEqual(wf["permissions"], {"contents": "read"})
        self.assertEqual(wf["jobs"]["review"]["permissions"], {"contents": "read", "pull-requests": "write"})

    def test_drafts_and_forks_wait_and_a_newer_push_replaces_a_running_review(self):
        wf = workflow(REVIEW)
        self.assertIn("!github.event.pull_request.draft", wf["jobs"]["review"]["if"])
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", wf["jobs"]["review"]["if"])
        self.assertEqual(wf["concurrency"], {"group": "pr-review-${{ github.event.pull_request.number }}",
                                             "cancel-in-progress": True})

    def test_self_heal_reviews_its_pull_request_from_a_fresh_checkout(self):
        jobs = workflow(SELF_HEAL)["jobs"]
        self.assertNotIn("secrets.BOB_API_KEY", str(jobs["repair"]))   # the job that ran the agent's code
        self.assertEqual(jobs["repair"]["outputs"], {"pull-branch": "${{ steps.run.outputs.pull-branch }}"})
        review = jobs["review"]
        self.assertEqual(review["needs"], "repair")
        self.assertEqual(review["permissions"], {"contents": "read", "pull-requests": "write"})
        step = pipeline_step_of({"jobs": {"review": review}}, "scripts/review_pr.py")
        self.assertEqual(step["run"], 'python3 scripts/review_pr.py --head "$PULL_BRANCH"')   # no ${{ }} in the script
        self.assertEqual(step["env"]["PULL_BRANCH"], "${{ needs.repair.outputs.pull-branch }}")

    def test_the_default_models_are_the_documented_ones(self):
        models = review_pr.DEFAULT_MODELS
        self.assertIn(f'prop review-models "{models}"', (helpers.REPO / "ci" / "lib.sh").read_text(encoding="utf-8"))
        docs = (helpers.REPO / "docs" / "pipelines.md").read_text(encoding="utf-8")
        for model in models.split():
            self.assertIn(f"`{model}`", docs)


def pipeline_step_of(wf: dict, script: str) -> dict:
    [step] = [s for s in steps(wf) if script in s.get("run", "")]
    return step


REMOTE = "github.com/hashicorp/agentic-clm"


class StateBranch(unittest.TestCase):
    """The build state kept on a branch of its own, for a base branch the pipeline cannot push to. The
    pipeline pushes to https://x-access-token@<origin>; git sends that to a local bare repository."""

    def setUp(self):
        tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.origin, self.work = tmp / "origin.git", tmp / "work"
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("GIT_", "CI_", "ICA_", "STATE_", "BASE_", "GITHUB_"))}
        self.env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", PYTHON=sys.executable,
                        GIT_AUTHOR_NAME="pipeline", GIT_AUTHOR_EMAIL="pipeline@example.invalid",
                        GIT_COMMITTER_NAME="pipeline", GIT_COMMITTER_EMAIL="pipeline@example.invalid")
        shutil.copytree(helpers.REPO / "ci", self.work / "ci")
        shutil.copytree(helpers.SCRIPTS / "lib", self.work / "scripts" / "lib",
                        ignore=shutil.ignore_patterns("__pycache__"))
        (self.work / "state").mkdir()
        (self.work / "state" / "blogs.json").write_text("seed\n", encoding="utf-8")
        self.git("init", "-q", "--bare", "-b", "main", str(self.origin), cwd=tmp)
        self.git("init", "-q", "-b", "main")
        self.git("add", "-A")
        self.git("commit", "-q", "-m", "seed")
        self.git("remote", "add", "origin", f"git@{REMOTE.replace('/', ':', 1)}.git")
        self.git("config", f"url.{self.origin.as_uri()}.insteadOf", f"https://x-access-token@{REMOTE}.git")
        self.git("push", "-q", str(self.origin), "main")
        self.seed = self.git("rev-parse", "main")

    def git(self, *args, cwd=None) -> str:
        return subprocess.run(["git", *args], cwd=cwd or self.work, env=self.env, check=True,
                              capture_output=True, text=True).stdout.strip()

    def at_origin(self, *args) -> str:
        return self.git("--git-dir", str(self.origin), *args)

    def ci(self, script: str, state_branch: str = "pipeline-state") -> str:
        env = {**self.env, "GIT_TOKEN": "t0ken", "STATE_BRANCH": state_branch, "BASE_BRANCH": "main"}
        proc = subprocess.run(["bash", "-c", f"source ci/lib.sh && ci_setup && {script}"],
                              cwd=self.work, env=env, capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc.stdout + proc.stderr

    def build(self, state: str) -> str:
        """A weekly build that leaves `state` in state/blogs.json."""
        return self.ci(f"ci_load_state && echo {state} > state/blogs.json && ci_commit_state 'chore(state): {state}'")

    def test_the_first_build_starts_the_branch_with_state_alone(self):
        out = self.build("week1")
        self.assertIn("no branch pipeline-state to read", out)
        self.assertIn("state/ pushed to pipeline-state", out)
        self.assertEqual(self.at_origin("ls-tree", "-r", "--name-only", "pipeline-state"), "state/blogs.json")
        self.assertEqual(self.at_origin("show", "pipeline-state:state/blogs.json"), "week1")
        self.assertEqual(self.at_origin("rev-parse", "main"), self.seed)   # the base branch is left alone

    def test_the_next_build_starts_from_the_branch_and_continues_it(self):
        self.build("week1")
        self.git("checkout", "-q", "--", "state/")                         # a fresh checkout of main
        out = self.ci("ci_load_state && cat state/blogs.json")
        self.assertIn("state/ from branch pipeline-state", out)
        self.assertIn("week1", out)
        self.assertEqual(self.git("diff", "--cached", "--name-only"), "")   # nothing for a code commit to take
        self.build("week2")
        self.assertEqual(self.at_origin("log", "--format=%s", "pipeline-state").splitlines(),
                         ["chore(state): week2", "chore(state): week1"])

    def test_a_reset_puts_back_the_state_the_run_started_from(self):
        self.build("week1")
        out = self.ci("ci_load_state && echo edited-by-an-agent > state/blogs.json && ci_reset_state && cat state/blogs.json")
        self.assertIn("week1", out)
        self.assertNotIn("edited-by-an-agent", out)

    def test_an_unchanged_state_commits_nothing(self):
        self.build("week1")
        self.assertIn("state/ unchanged", self.build("week1"))
        self.assertEqual(self.at_origin("rev-list", "--count", "pipeline-state"), "1")

    def test_a_build_from_an_older_state_does_not_replace_a_newer_one(self):
        self.build("week1")
        self.ci("ci_load_state")
        newer = self.git("commit-tree", "refs/ci/state^{tree}",
                         "-p", "refs/ci/state", "-m", "pushed by another run")
        self.git("push", "-q", str(self.origin), f"{newer}:refs/heads/pipeline-state")
        out = self.ci("echo week2 > state/blogs.json && ci_commit_state 'chore(state): week2'")
        self.assertIn("could not push state/ to pipeline-state", out)
        self.assertEqual(self.at_origin("rev-parse", "pipeline-state"), newer)

    def test_without_a_branch_of_its_own_the_state_goes_to_the_base_branch(self):
        for setting in ("", "main"):
            with self.subTest(state_branch=setting):
                out = self.ci(f"ci_load_state && echo {setting or 'unset'} > state/blogs.json "
                              f"&& ci_commit_state 'chore(state): {setting or 'unset'}'", state_branch=setting)
                self.assertIn("state/ pushed to main", out)
                self.assertEqual(self.at_origin("show", "main:state/blogs.json"), setting or "unset")
        self.assertEqual(self.at_origin("branch", "--list", "pipeline-state"), "")


class Models(unittest.TestCase):
    def test_the_documented_models_are_the_ones_ci_falls_back_to(self):
        # When the gateway stops serving a model, the switch has to reach every place that names it.
        docs = (helpers.REPO / "docs" / "pipelines.md").read_text(encoding="utf-8")
        example = (helpers.REPO / ".env.example").read_text(encoding="utf-8")
        defaults = ci_defaults()
        for setting in ("enrich-model", "fix-model"):
            model, var = defaults[setting], setting.upper().replace("-", "_")
            self.assertIn(f"`{setting}`, default `{model}`", docs, setting)      # the overview table
            self.assertIn(f"| `{setting}` | text | `{model}` |", docs, setting)   # the SPS properties
            self.assertRegex(example, rf"(?m)^(# )?{var}={re.escape(model)}\s", setting)


KEY = "sk-current-0123456789abcdWXYZ"
KEY_HASH = "becb5b6835b04bfce93eb60ed6d2ef98306d58e54f6e050b3e6dd8ef34167e04"
BOB_KEY = "bob-inference-0123456789abcdefghij"


class FakeGateway(BaseHTTPRequestHandler):
    """Answers as the ICA gateway did on 2026-10-20: 401 for any other key (echoing its last characters
    and its hash), 400 E003 for a model it no longer serves, 200 otherwise. Bob's inference key, sent as
    "Apikey", is accepted too."""
    requests: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeGateway.requests.append((self.path, self.headers, body))
        if self.headers["Authorization"] not in (f"Bearer {KEY}", f"Apikey {BOB_KEY}"):
            status, message = 401, ("Authentication Error, Invalid proxy server token passed. Received API Key = "
                                    f"sk-...{self.headers['Authorization'][-4:]}, Key Hash (Token) ={KEY_HASH}. "
                                    "Unable to find token in cache")
        elif body["model"] == "behind-a-firewall":
            data = b"<!DOCTYPE html><html><head><title>Attention Required! | Cloudflare</title></head></html>"
            self.send_response(403)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        elif body["model"] == "claude-withdrawn":
            status, message = 400, "E003: Model not available for Coding Agents. Please use Advantage Assist models"
        else:
            status, message = 200, ""
        reply = {"choices": [{"message": {"role": "assistant", "content": f"answer from {body['model']}"}}]}
        data = json.dumps({"error": {"message": message}} if message else reply).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


class GatewayKey(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeGateway)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}/v1"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        FakeGateway.requests.clear()

    def preflight(self, **env) -> tuple[int, str]:
        env = {"ICA_API_KEY": KEY, "ICA_BASE_URL": self.base, "ENRICH_MODEL": "ica-gateway/claude-small",
               "FIX_MODEL": "ica-gateway/claude-large", **env}
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = gateway.preflight(env)
        return code, out.getvalue()

    def test_a_key_accepted_for_both_models_passes(self):
        code, out = self.preflight()
        self.assertEqual(code, 0)
        self.assertEqual(out.count("accepted by the gateway"), 2)
        [(path, headers, body), _] = FakeGateway.requests
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual(headers["Authorization"], f"Bearer {KEY}")
        self.assertNotIn("Python-urllib", headers["User-Agent"])   # Cloudflare turns that away
        self.assertEqual((body["model"], body["max_tokens"]), ("claude-small", 1))

    def test_a_withdrawn_model_fails_the_check_with_the_gateway_s_reason(self):
        code, out = self.preflight(FIX_MODEL="ica-gateway/claude-withdrawn", GITHUB_ACTIONS="true")
        self.assertEqual(code, 1)
        self.assertIn("ica-gateway/claude-withdrawn", out)
        self.assertIn("REFUSED by the gateway (HTTP 400: E003: Model not available for Coding Agents", out)
        self.assertIn("::warning::ICA_API_KEY not confirmed for ica-gateway/claude-withdrawn", out)

    def test_a_rotated_key_is_reported_without_any_part_of_it(self):
        old = "sk-rotated-9876543210zyxwQRST"
        code, out = self.preflight(ICA_API_KEY=old)
        self.assertEqual(code, 1)
        self.assertIn("HTTP 401: Authentication Error", out)
        for fragment in (old, old[-4:], KEY_HASH):
            self.assertNotIn(fragment, out)

    def test_without_a_key_nothing_is_asked_and_nothing_fails(self):
        self.assertEqual(self.preflight(ICA_API_KEY=""), (0, ""))
        self.assertEqual(FakeGateway.requests, [])

    def test_a_review_model_is_answered_through_its_own_gateway(self):
        messages = [{"role": "user", "content": "Review this."}]
        env = {"ICA_API_KEY": KEY, "ICA_BASE_URL": self.base, "BOB_API_KEY": BOB_KEY, "BOB_BASE_URL": self.base}
        self.assertEqual(gateway.complete("ica-gateway/gpt-6.1-sol", messages, env), (200, "answer from gpt-6.1-sol"))
        self.assertEqual(gateway.complete("bob/premium", messages, env), (200, "answer from premium"))
        [(_, ica, ica_body), (_, bob, bob_body)] = FakeGateway.requests
        self.assertEqual((ica["Authorization"], bob["Authorization"]), (f"Bearer {KEY}", f"Apikey {BOB_KEY}"))
        self.assertEqual((ica_body["messages"], bob_body["model"]), (messages, "premium"))

    def test_a_refused_call_says_why_without_the_key(self):
        old = "sk-rotated-9876543210zyxwQRST"
        status, said = gateway.complete("ica-gateway/gpt-6.1-sol", [], {"ICA_API_KEY": old, "ICA_BASE_URL": self.base})
        self.assertEqual(status, 401)
        for fragment in (old, old[-4:], KEY_HASH):
            self.assertNotIn(fragment, said)

    def test_a_firewall_s_page_is_named_not_pasted(self):
        status, said = gateway.complete("bob/behind-a-firewall", [], {"BOB_API_KEY": BOB_KEY, "BOB_BASE_URL": self.base})
        self.assertEqual((status, said), (403, "Attention Required! | Cloudflare (an HTML page, not the API)"))

    def test_a_provider_without_its_key_is_not_asked(self):
        self.assertEqual(gateway.complete("bob/premium", [], {"ICA_API_KEY": KEY}), (None, "no BOB_API_KEY set"))
        self.assertEqual(FakeGateway.requests, [])

    def test_the_preflight_also_checks_the_review_models_it_has_a_key_for(self):
        code, out = self.preflight(REVIEW_MODELS="ica-gateway/gpt-review bob/premium")
        self.assertEqual(code, 0)
        self.assertIn("ica-gateway/gpt-review", out)
        self.assertNotIn("bob/premium", out)                          # no BOB_API_KEY in this run
        self.assertEqual(len(FakeGateway.requests), 3)

    def test_an_unreachable_gateway_fails_the_check(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]   # nothing listens there once the socket is closed
        code, out = self.preflight(ICA_BASE_URL=f"http://127.0.0.1:{port}/v1")
        self.assertEqual(code, 1)
        self.assertEqual(out.count("NO ANSWER from the gateway"), 2)   # not "refused": it never said


if __name__ == "__main__":
    unittest.main()
