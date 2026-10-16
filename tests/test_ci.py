"""The pipelines on GitHub Actions: the workflow files and the hand-over from ci/ to the self-heal job.

These are the mistakes that would otherwise only show on GitHub, after a failed Monday run: a self-heal
job that waits for an output nobody writes, a token left behind for the repair agent to find, a
setting the scripts never read.
"""
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

import helpers

GITHUB = helpers.REPO / ".github"
WEEKLY = "weekly-deck.yml"
SELF_HEAL = "self-heal.yml"
# Set by the workflow for the scripts, but not pipeline properties.
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
        granted = workflow(WEEKLY)["jobs"]["self-heal"]["permissions"]
        for job in workflow(SELF_HEAL)["jobs"].values():
            for scope, level in job["permissions"].items():
                self.assertEqual(granted.get(scope), level, scope)


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
        for name in (WEEKLY, SELF_HEAL):
            for step in steps(workflow(name)):
                if step.get("uses", "").startswith("actions/checkout@"):
                    self.assertIs(step.get("with", {}).get("persist-credentials"), False, name)

    def test_secrets_reach_only_the_pipeline_step(self):
        for name in (WEEKLY, SELF_HEAL):
            wf = workflow(name)
            self.assertEqual(wf["permissions"], {"contents": "read"}, name)
            self.assertNotIn("secrets.", str(wf.get("env", {})), name)
            for job in wf["jobs"].values():
                self.assertNotIn("secrets.", str(job.get("env", {})), name)
            for step in steps(wf):
                self.assertNotIn("secrets.", step.get("run", ""), name)   # never pasted into a script
                if step is not pipeline_step(wf):
                    self.assertNotIn("secrets.", str(step.get("env", {})), name)

    def test_the_runner_installs_the_image_s_opencode_version(self):
        action = (GITHUB / "actions" / "setup" / "action.yml").read_text(encoding="utf-8")
        self.assertIn("s/^ARG OPENCODE_VERSION=//p' Dockerfile", action)
        self.assertRegex((helpers.REPO / "Dockerfile").read_text(encoding="utf-8"),
                         r"(?m)^ARG OPENCODE_VERSION=\d+\.\d+\.\d+$")


if __name__ == "__main__":
    unittest.main()
