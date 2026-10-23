"""IBM Bob as every model step's last resort (lib/bob.py): what Bob Shell is given, what it may do, and
that the wording, the review and the repair agent turn to it when the ICA gateway gives no answer."""
import json
import os
import pwd
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers
import enrich
import review_pr
import run_agent
from lib import agent, bob, gateway, sandbox

# Stands in for Bob Shell: records its arguments, environment and settings, and answers as `bob run -f
# json` does. FAKE_BOB=fail answers with an error; otherwise a wording request gets one bullet per item.
FAKE_BOB = """#!{python}
import json, os, sys
from pathlib import Path
prompt = sys.stdin.read()
settings = json.loads((Path(os.environ["HOME"]) / ".bob" / "settings.json").read_text())
Path(os.environ["FAKE_BOB_LOG"]).write_text(json.dumps({{"args": sys.argv[1:], "prompt": prompt, "settings": settings,
    "env": sorted(k for k in os.environ if k.endswith(("_KEY", "_TOKEN"))),
    "key_ok": os.environ.get("BOB_API_KEY") == "bob-key-0123456789abcdefghij", "cwd": os.getcwd()}}))
if os.environ.get("FAKE_BOB") == "fail":
    print(json.dumps({{"type": "result", "status": "error", "error": "quota exceeded", "stats": {{}}}}))
    sys.exit(1)
reply = "OK"
try:
    items = json.loads(prompt.rsplit("\\n\\n---\\n\\n", 1)[1])["items"]
    reply = json.dumps({{"items": [{{"id": i["id"], "bullets": ["Adds what its release notes describe, in one plain sentence."]}} for i in items]}})
except (IndexError, ValueError, KeyError, TypeError):
    pass
print(json.dumps({{"type": "result", "status": "success", "last_message": reply, "stats": {{"session_costs": 0.01}}}}))
"""
KEYS = {"BOB_API_KEY": "bob-key-0123456789abcdefghij", "ICA_API_KEY": "sk-ica-0123456789abcdef", "GIT_TOKEN": "ghs_0123456789abcdef"}


class BobShell(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        fake = self.tmp / "bob"
        fake.write_text(FAKE_BOB.format(python=sys.executable), encoding="utf-8")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        self.log = self.tmp / "bob-call.json"
        self.enterContext(mock.patch.dict(os.environ, {"BOB_SHELL": str(fake), "FAKE_BOB_LOG": str(self.log), **KEYS}))

    def call(self) -> dict:
        return json.loads(self.log.read_text(encoding="utf-8"))

    def test_a_question_runs_in_ask_mode_with_no_tools_and_only_bob_s_key(self):
        self.assertEqual(gateway.complete("bob/shell", [{"role": "system", "content": "Be brief."},
                                                         {"role": "user", "content": "Reply OK"}]), (200, "OK"))
        call = self.call()
        args = call["args"]
        self.assertEqual(args[:3], ["run", "--mode", "ask"])
        self.assertEqual(args[args.index("--disable-tool-groups") + 1].split(","), list(bob.TOOL_GROUPS))
        self.assertIn("--accept-license", args)
        self.assertEqual(args[args.index("-f") + 1], "json")
        self.assertEqual(call["prompt"], gateway.NO_TOOLS + "Be brief.\n\n---\n\nReply OK")
        self.assertEqual(call["env"], ["BOB_API_KEY"])                     # not the gateway key, not the token
        self.assertEqual(call["settings"]["bobShell"], {"autoUpdate": "none"})
        self.assertEqual(call["settings"]["telemetry"], {"enabled": False, "excludePayload": True})

    def test_bob_s_failure_is_reported_without_its_key(self):
        with mock.patch.dict(os.environ, {"FAKE_BOB": "fail"}):
            status, said = gateway.complete("bob/shell", [{"role": "user", "content": "x"}])
        self.assertIsNone(status)
        self.assertEqual(said, "Bob Shell: error: quota exceeded")

    def test_without_its_key_bob_is_not_run(self):
        with mock.patch.dict(os.environ, {"BOB_API_KEY": ""}):
            self.assertEqual(gateway.complete("bob/shell", []), (None, "no BOB_API_KEY set"))
        self.assertFalse(self.log.exists())

    def test_bob_is_the_last_resort_only_when_its_key_is_set(self):
        models = ["ica-gateway/a", "ica-gateway/b"]
        self.assertEqual(gateway.with_last_resort(models, {"BOB_API_KEY": "k"}), models + ["bob/shell"])
        self.assertEqual(gateway.with_last_resort(models, {}), models)
        self.assertEqual(gateway.with_last_resort(models, {"BOB_API_KEY": "k", "BOB_FALLBACK": "0"}), models)

    def test_the_repair_agent_on_bob_reads_and_edits_but_runs_no_command(self):
        # Bob's key cannot be relayed (scripts/key_relay.py), so no command may ever run next to it.
        code, model = agent.run_first("repo-fixer", "fix it", models=["bob/shell"], answered=lambda code: code == 0,
                                      cwd=self.tmp)
        self.assertEqual((code, model), (0, "bob/shell"))
        call = self.call()
        args = call["args"]
        self.assertEqual(args[args.index("--mode") + 1], "agent")
        self.assertIn("execute", args[args.index("--disable-tool-groups") + 1].split(","))
        approval = call["settings"]["approval"]
        self.assertEqual(approval["allowed_permissions"], ["read", "edit"])
        self.assertNotIn("allowedExecutors", approval)
        self.assertIn("Repairs the auto-pptx repository", (helpers.REPO / ".opencode/agents/repo-fixer.md").read_text())
        self.assertTrue(call["prompt"].endswith("\n\n---\n\n" + agent.NO_COMMANDS + "fix it"))
        self.assertEqual(call["env"], ["BOB_API_KEY"])

    def test_execute_is_never_granted_whoever_asks(self):
        bob.run("x", workspace=self.tmp, mode="agent", tools=("read", "edit", "execute"))
        self.assertIn("execute", self.call()["args"][self.call()["args"].index("--disable-tool-groups") + 1])
        self.assertEqual(self.call()["settings"]["approval"]["allowed_permissions"], ["read", "edit"])

    def test_in_the_sandbox_bob_reads_its_key_from_its_input_alone(self):
        ran = []

        def without_sudo(cmd, *, cwd, env):   # what sandbox.command runs after sudo switches user
            line = [*(f"{k}={v}" for k, v in sorted(env.items()))]
            ran.append(line)
            return ["env", "-i", *line, "/bin/bash", "-c", 'cd "$1" && shift && exec "$@"', "sandbox", str(cwd), *cmd]

        work = self.tmp / "repo"
        (work / "output").mkdir(parents=True)
        (work / "leads-out").symlink_to("/etc")
        with mock.patch.dict(os.environ, {"SANDBOX_USER": "sandbox-user"}), \
                mock.patch.object(sandbox, "command", without_sudo), \
                mock.patch.object(sandbox, "give"), mock.patch.object(sandbox, "take"), \
                mock.patch.object(sandbox, "clear") as clear, \
                mock.patch.object(sandbox.pwd, "getpwnam", return_value=mock.Mock(pw_dir=str(self.tmp))):
            agent.run_first("repo-fixer", "fix it", models=["bob/shell"], answered=lambda code: True, cwd=work)
        call = self.call()
        self.assertTrue(call["key_ok"])                                       # Bob has its key...
        self.assertFalse(any(v in arg for line in ran for arg in line for v in KEYS.values()))   # ...never on a command line
        self.assertNotIn(KEYS["BOB_API_KEY"], call["prompt"])
        self.assertTrue(call["prompt"].endswith("fix it"))
        self.assertEqual(call["env"], ["BOB_API_KEY"])
        self.assertEqual(Path(call["cwd"]).resolve(), work.resolve())
        self.assertFalse((work / "leads-out").is_symlink())                   # no link leads Bob out of its copy
        self.assertTrue(clear.called)


    def test_the_wording_agent_on_bob_may_read_and_edit_but_run_nothing(self):
        agent.run_first("deck-enrich", "word it", models=["bob/shell"], answered=lambda code: True, cwd=self.tmp)
        self.assertEqual(self.call()["settings"]["approval"]["allowed_permissions"], ["read", "edit"])

    def test_the_wording_turns_to_bob_when_the_gateway_gives_no_answer(self):
        request = {"items": [{"id": "vault/2.1.0", "kind": "release", "max_bullets": 1, "max_chars": 230,
                              "source": {"notes": {"Features": ["Adds what its release notes describe"]}}}]}
        silent = lambda model, messages, env: ((None, "unreachable") if model.startswith("ica-gateway/")
                                               else gateway.complete(model, messages, env))
        answers, model, failures = enrich.ask_direct(request, dict(os.environ), complete=silent)
        self.assertEqual((model, failures[1]), ("bob/shell", "ica-gateway/gpt-6-luna: not asked, ica-gateway gave no answer"))
        self.assertEqual(list(answers), ["vault/2.1.0"])

    def test_the_review_turns_to_bob_when_the_gateway_gives_no_answer(self):
        reply = json.dumps({"verdict": "safe", "summary": "Fine.", "findings": []})
        ask = lambda model, messages, env: (200, reply) if model == "bob/shell" else (None, "unreachable")
        answer, model, failures = review_pr.ask_models([{"role": "user", "content": "x"}], dict(os.environ), ask)
        self.assertEqual((answer["verdict"], model), ("safe", "bob/shell"))
        self.assertEqual(failures, ["ica-gateway/gpt-6.1-sol: unreachable", "ica-gateway/claude-sonnet-5-5: not asked, "
                                    "ica-gateway gave no answer"])

    def test_bob_gets_a_second_try_when_its_answer_is_not_the_json_asked_for(self):
        replies = iter([(200, "<invoke name=\"read_file\">"), (200, json.dumps({"verdict": "safe", "findings": []}))])
        ask = lambda model, messages, env: next(replies) if model == "bob/shell" else (None, "unreachable")
        answer, model, _ = review_pr.ask_models([{"role": "user", "content": "x"}], dict(os.environ), ask)
        self.assertEqual((answer["verdict"], model), ("safe", "bob/shell"))

    def test_a_zero_exit_without_the_agent_s_answer_hands_over_to_the_next_model(self):
        # opencode can exit 0 when the model was refused; only the summary file shows an answer.
        summary = self.tmp / "fix-summary.md"
        ran = []

        def fake_run(name, message, *, model, cwd, timeout, log_file, drop_env):
            ran.append(model)
            if model == "ica-gateway/b":
                summary.write_text("Fixed it.")
            return 0

        with mock.patch.object(run_agent.agent, "available", return_value=True), \
                mock.patch.object(run_agent.agent, "run", fake_run), \
                mock.patch.object(sys, "argv", ["run_agent.py", "repo-fixer", "--model", "ica-gateway/a",
                                                "--fallback-model", "ica-gateway/b", "--answered-file", str(summary),
                                                "--message", "m", "--log", str(self.tmp / "a.log")]):
            run_agent.main()
        self.assertEqual(ran, ["ica-gateway/a", "ica-gateway/b"])   # and not Bob: b answered

    def test_the_repair_chain_ends_with_bob(self):
        with mock.patch.object(run_agent.agent, "available", return_value=True), \
                mock.patch.object(run_agent.agent, "run_first", return_value=(0, "bob/shell")) as first, \
                mock.patch.object(sys, "argv", ["run_agent.py", "repo-fixer", "--model", "ica-gateway/a",
                                                "--fallback-model", "ica-gateway/b", "--message", "m",
                                                "--log", str(self.tmp / "a.log")]):
            run_agent.main()
        self.assertEqual(first.call_args.kwargs["models"], ["ica-gateway/a", "ica-gateway/b", "bob/shell"])


class Install(unittest.TestCase):
    def test_a_package_that_is_not_the_one_pinned_is_never_installed(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"not the package"
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, {"BOB_SHELL_HOME": tmp, "BOB_SHELL": "", "BOB_SHELL_INSTALL": "1"}), \
                mock.patch.object(bob, "node_version", return_value=(22, 20)), \
                mock.patch.object(bob.shutil, "which", return_value="/usr/bin/npm"), \
                mock.patch.object(bob.urllib.request, "urlopen", return_value=Response()), \
                mock.patch.object(bob.subprocess, "run") as npm:
            with self.assertRaisesRegex(bob.BobError, "is not the package pinned"):
                bob.install()
        npm.assert_not_called()

    def test_an_old_node_is_said_plainly(self):
        with mock.patch.dict(os.environ, {"BOB_SHELL": "", "BOB_SHELL_INSTALL": "1", "BOB_SHELL_HOME": "/nonexistent"}), \
                mock.patch.object(bob, "node_version", return_value=(20, 11)):
            with self.assertRaisesRegex(bob.BobError, "Node 22.15 or later"):
                bob.install()

    def test_the_tests_never_see_the_pipeline_s_real_keys(self):
        # Self-heal tests a repair with its keys set; with them, the steps would take other paths.
        for name in ("ICA_API_KEY", "ICA_BASE_URL", "BOB_API_KEY"):
            self.assertNotIn(name, os.environ)

    def test_the_tests_never_install_it(self):
        with mock.patch.dict(os.environ, {"BOB_SHELL": "", "BOB_SHELL_HOME": "/nonexistent"}):
            with self.assertRaisesRegex(bob.BobError, "BOB_SHELL_INSTALL=0"):
                bob.install()


if __name__ == "__main__":
    unittest.main()
