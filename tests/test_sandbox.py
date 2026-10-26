"""The self-heal sandbox: the repair agent and the code it writes run as another user without the
pipeline's secrets (lib/sandbox.py), reach the gateway through a relay that holds the key
(scripts/key_relay.py), and work in a copy whose change is judged and committed elsewhere (ci/lib.sh,
scripts/check_fix.py --tree). Nothing here needs sudo: the user switch itself is left out."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import helpers

import key_relay
import run_agent
from lib import agent, gateway, sandbox

KEY = "sk-ica-real-0123456789abcdef"
SECRETS = {"ICA_API_KEY": KEY, "BOB_API_KEY": "bob-key-0123456789abcdefghij", "GIT_TOKEN": "ghs_0123456789abcdef",
           "GITHUB_TOKEN": "ghs_abcdef0123456789", "ACTIONS_RUNTIME_TOKEN": "eyJ0eXAi", "SOME_PASSWORD": "hunter2hunter2"}


class Environment(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        relay_env = self.tmp / "relay.env"
        relay_env.write_text("ICA_BASE_URL=http://127.0.0.1:4321\nICA_API_KEY=relay-ica-token\n")
        self.env = {**SECRETS, "PATH": "/usr/bin:/bin", "FIX_MODEL": "ica-gateway/claude-sonnet-5-5",
                    "ICA_BASE_URL": "https://gateway.example/v1", "HOME": "/home/runner", "GITHUB_ENV": "/x/env",
                    "OPENCODE_CONFIG_CONTENT": "{}", "SANDBOX_USER": "sandbox-user",
                    "SANDBOX_RELAY_ENV": str(relay_env), "BASH_FUNC_x%%": "() { :; }"}
        self.enterContext(mock.patch.object(sandbox.pwd, "getpwnam", return_value=mock.Mock(pw_dir="/home/sandbox-user")))

    def test_the_sandbox_gets_no_secret_and_the_relay_s_stand_ins(self):
        env = sandbox.environment(self.env)
        for name in SECRETS:
            self.assertNotEqual(env.get(name), SECRETS[name], name)
        self.assertEqual(env["ICA_API_KEY"], "relay-ica-token")
        self.assertEqual(env["ICA_BASE_URL"], "http://127.0.0.1:4321")
        self.assertEqual((env["HOME"], env["USER"]), ("/home/sandbox-user", "sandbox-user"))
        for name in ("GITHUB_ENV", "OPENCODE_CONFIG_CONTENT", "SANDBOX_USER", "SANDBOX_RELAY_ENV", "BASH_FUNC_x%%"):
            self.assertNotIn(name, env)
        self.assertEqual((env["PATH"], env["FIX_MODEL"]), ("/usr/bin:/bin", "ica-gateway/claude-sonnet-5-5"))

    def test_a_command_switches_user_and_starts_from_an_empty_environment(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            cmd = sandbox.command(["python", "-V"], cwd=Path("/opt/auto-pptx/repo"), env=sandbox.environment())
        self.assertEqual(cmd[:7], ["sudo", "-n", "-u", "sandbox-user", "--", "env", "-i"])
        self.assertEqual(cmd[-4:], ["sandbox", "/opt/auto-pptx/repo", "python", "-V"])
        self.assertFalse(any(value in arg for arg in cmd for value in SECRETS.values()))

    def test_the_repair_agent_runs_in_the_sandbox_on_the_relay(self):
        seen = {}

        def fake_run(cmd, *, cwd, env, **kwargs):
            seen.update(cmd=cmd, cwd=cwd, env=env)
            return subprocess.CompletedProcess(cmd, 0, stdout="done")

        with mock.patch.dict(os.environ, self.env, clear=True), mock.patch.object(sandbox, "run", fake_run):
            code = agent.run("repo-fixer", "fix it", model="ica-gateway/claude-sonnet-5-5", cwd=self.tmp)
        self.assertEqual(code, 0)
        self.assertEqual(seen["cwd"], self.tmp)
        self.assertEqual(seen["env"]["OPENCODE_CONFIG_DIR"], str(self.tmp / ".opencode"))
        provider = json.loads(seen["env"]["OPENCODE_CONFIG_CONTENT"])["provider"]["ica-gateway"]
        self.assertEqual(provider["options"]["baseURL"], "http://127.0.0.1:4321")
        self.assertEqual(provider["options"]["apiKey"], "{env:ICA_API_KEY}")
        self.assertFalse(any(value in json.dumps(seen, default=str) for value in SECRETS.values()))

    def test_without_a_sandbox_nothing_changes_hands(self):
        with mock.patch.dict(os.environ, {"PATH": os.environ["PATH"]}, clear=True):
            self.assertFalse(sandbox.active())
            done = sandbox.run([sys.executable, "-c", "import os; print(os.getcwd())"], cwd=self.tmp,
                               capture_output=True, text=True)
        self.assertEqual(Path(done.stdout.strip()).resolve(), self.tmp.resolve())

    def test_an_answer_is_looked_for_in_the_agent_s_own_directory(self):
        work = self.tmp / "copy"
        (work / "output").mkdir(parents=True)
        tried = []

        def fake_run(name, message, *, model, cwd, timeout, log_file, drop_env):
            tried.append((model, cwd))
            if model == "ica-gateway/b":
                (work / "output" / "fix-summary.md").write_text("Fixed.")
            return 0

        with mock.patch.object(run_agent.agent, "available", return_value=True), \
                mock.patch.object(run_agent.agent, "run", fake_run), \
                mock.patch.object(sys, "argv", ["run_agent.py", "repo-fixer", "--dir", str(work), "--model",
                                                "ica-gateway/a", "--fallback-model", "ica-gateway/b",
                                                "--answered-file", "output/fix-summary.md", "--message", "m",
                                                "--log", str(self.tmp / "a.log")]):
            run_agent.main()
        self.assertEqual(tried, [("ica-gateway/a", work), ("ica-gateway/b", work)])


class Upstream(BaseHTTPRequestHandler):
    """The gateway: answers a chat in two pieces (echoing its key, cut in two, when asked to), and a refused
    key by echoing it, as the ICA gateway does."""
    protocol_version = "HTTP/1.0"
    seen: list = []

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        Upstream.seen.append((self.path, self.headers["Authorization"], json.loads(body)))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        said = self.headers["Authorization"].split()[-1] if json.loads(body).get("echo") else "OK"
        answer = json.dumps({"choices": [{"message": {"content": said}}]}).encode()
        cut = answer.index(b'"content": "') + 14   # two characters into what it says
        self.wfile.write(answer[:cut])
        self.wfile.flush()
        time.sleep(0.1)
        self.wfile.write(answer[cut:])

    def do_GET(self):
        Upstream.seen.append((self.path, self.headers["Authorization"], None))
        self.send_response(401)
        self.end_headers()
        self.wfile.write(f"Authentication Error, key {self.headers['Authorization'].split()[-1]} is not valid".encode())

    def log_message(self, *args):
        pass


class Relay(unittest.TestCase):
    def setUp(self):
        Upstream.seen = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        [self.relay] = key_relay.start({"ICA_API_KEY": KEY, "ICA_BASE_URL": f"http://127.0.0.1:{server.server_port}/v1"})
        self.addCleanup(self.relay.server.server_close)
        self.addCleanup(self.relay.server.shutdown)
        self.stand_ins = key_relay.stand_ins([self.relay])

    def call(self, path: str, token: str | None, method: str = "GET", body: bytes = b"{}") -> tuple[int, str]:
        req = urllib.request.Request(self.relay.url + path, method=method, data=body if method == "POST" else None,
                                     headers={"Authorization": f"Bearer {token}"} if token else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read().decode()

    def test_the_stand_ins_name_the_relay_and_its_token_never_the_key(self):
        self.assertEqual(set(self.stand_ins), {"ICA_BASE_URL", "ICA_API_KEY"})
        self.assertEqual(self.stand_ins["ICA_BASE_URL"], self.relay.url)
        self.assertNotIn(KEY, json.dumps(self.stand_ins))

    def test_a_model_call_with_the_token_reaches_the_gateway_with_the_key(self):
        status, said = gateway.complete("ica-gateway/claude-haiku-5-5", [{"role": "user", "content": "Reply OK"}],
                                        dict(self.stand_ins))
        self.assertEqual((status, said), (200, "OK"))
        [(path, auth, body)] = Upstream.seen
        self.assertEqual((path, auth, body["model"]), ("/v1/chat/completions", f"Bearer {KEY}", "claude-haiku-5-5"))

    def test_the_key_a_refusal_echoes_does_not_come_back(self):
        status, said = self.call("/models", self.relay.token)
        self.assertEqual(status, 401)
        self.assertNotIn(KEY, said)
        self.assertIn("[ica key removed]", said)

    def test_a_key_cut_across_two_pieces_of_an_answer_does_not_come_back(self):
        status, said = self.call("/chat/completions", self.relay.token, "POST", b'{"echo": true}')
        self.assertEqual(status, 200)
        self.assertNotIn(KEY, said)
        self.assertEqual(json.loads(said)["choices"][0]["message"]["content"], "[ica key removed]")

    def test_without_the_token_or_off_the_model_paths_nothing_is_sent(self):
        self.assertEqual(self.call("/models", None)[0], 403)
        self.assertEqual(self.call("/models", "relay-ica-guess")[0], 403)
        self.assertEqual(self.call("/key/info", self.relay.token)[0], 404)
        self.assertEqual(self.call("/v1/../chat/completions", self.relay.token, "POST")[0], 404)
        self.assertEqual(Upstream.seen, [])

    def test_no_key_no_relay(self):
        self.assertEqual(key_relay.start({"ICA_BASE_URL": "https://gateway.example/v1"}), [])


def git(tree: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(tree), *args], capture_output=True, text=True, check=True).stdout


def repository(root: Path, *, runner: bool = False) -> Path:
    """A small repository: some code, a protected file, generated folders (and, with `runner`, what
    ci_sandbox_run needs: scripts/run_sandboxed.py and scripts/lib)."""
    if runner:
        shutil.copytree(helpers.SCRIPTS / "lib", root / "scripts" / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        shutil.copy(helpers.SCRIPTS / "run_sandboxed.py", root / "scripts" / "run_sandboxed.py")
    for path, text in {"scripts/plan_deck.py": "x = 1\n", "scripts/lib/bob.py": "VERSION = 1\n",
                       "requirements.txt": "pyyaml\n", "state/history.json": "{}\n", "README.md": "hi\n"}.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text)
    git(root, "init", "-q")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A")
    git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    (root / "output").mkdir()
    (root / "data").mkdir()
    return root


class StagedGuard(unittest.TestCase):
    """check_fix.py --tree judges the change in a tree of its own."""

    def setUp(self):
        self.tree = repository(Path(self.enterContext(tempfile.TemporaryDirectory())))
        (self.tree / "output" / "fix-summary.md").write_text("Fixed the planner\n")

    def verdict(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in SECRETS}
        done = subprocess.run([sys.executable, str(helpers.SCRIPTS / "check_fix.py"), "--tree", str(self.tree)],
                              capture_output=True, text=True, env=env)
        return json.loads(done.stdout)

    def test_a_plain_repair_is_accepted(self):
        (self.tree / "scripts/plan_deck.py").write_text("x = 2\n")
        v = self.verdict()
        self.assertEqual((v["verdict"], v["files"], v["headline"]), ("ok", ["scripts/plan_deck.py"], "Fixed the planner"))

    def test_bob_s_box_and_the_requirements_are_protected(self):
        for path in ("scripts/lib/bob.py", "requirements.txt"):
            git(self.tree, "checkout", "-q", "--", ".")
            (self.tree / path).write_text("changed\n")
            self.assertEqual(self.verdict()["reasons"], [f"protected path changed: {path}"], path)

    def test_a_link_is_rejected_and_never_followed(self):
        (self.tree / "scripts" / "notes.txt").symlink_to("/etc/hosts")
        v = self.verdict()
        self.assertIn("a symbolic link: scripts/notes.txt", v["reasons"])
        self.assertEqual(v["lines"], 0)

    def test_a_summary_that_is_a_link_is_no_summary(self):
        (self.tree / "scripts/plan_deck.py").write_text("x = 2\n")
        summary = self.tree / "output" / "fix-summary.md"
        summary.unlink()
        summary.symlink_to(self.tree / "README.md")
        v = self.verdict()
        self.assertIn("a symbolic link: output/fix-summary.md", v["reasons"])
        self.assertIn("output/fix-summary.md is missing", v["reasons"])


class CopyAndStage(unittest.TestCase):
    """ci/lib.sh's sandbox helpers without the sandbox (no sudo here): the agent's copy, the stage, and
    what crosses between them."""

    def sh(self, script: str) -> subprocess.CompletedProcess:
        # Not the run's own settings either: inside a self-heal rehearsal CI_DRY_RUN=1 keeps the stage.
        env = {k: v for k, v in os.environ.items() if k not in SECRETS and k not in ("GITHUB_ACTIONS", "CI_DRY_RUN")}
        env.update(SELFHEAL_SANDBOX="off", PY=sys.executable)
        return subprocess.run(["bash", "-c", f'set -uo pipefail; source "{helpers.REPO}/ci/lib.sh"; PY="$PY"; {script}'],
                              cwd=self.tree, capture_output=True, text=True, env=env)

    def setUp(self):
        self.tree = repository(Path(self.enterContext(tempfile.TemporaryDirectory())), runner=True)
        (self.tree / "output" / "failure.json").write_text('{"step": "fetch-blogs"}')
        (self.tree / "data" / "blogs.json").write_text("[]")

    def test_the_change_is_judged_in_the_stage_and_this_checkout_is_never_touched(self):
        done = self.sh('''
            ci_sandbox_start && ci_sandbox_copy_in || exit 9
            test -f "$SANDBOX_DIR/output/failure.json" && test -f "$SANDBOX_DIR/data/blogs.json" || exit 8
            # the agent at work in its copy
            echo "x = 2" > "$SANDBOX_DIR/scripts/plan_deck.py"
            echo "new" > "$SANDBOX_DIR/scripts/new.py"
            rm "$SANDBOX_DIR/README.md"
            ln -s /etc/hosts "$SANDBOX_DIR/scripts/hosts"
            echo "Fixed it" > "$SANDBOX_DIR/output/fix-summary.md"
            echo "noise" > "$SANDBOX_DIR/output/agent.log"
            echo "{}" > "$SANDBOX_DIR/data/blogs.json"
            echo '{"edited": true}' > "$SANDBOX_DIR/state/history.json"
            ci_sandbox_collect || exit 7
            echo "STAGE=$STAGE"
            git -C "$STAGE" status --porcelain=v1 -uall
            ci_sandbox_stop
            if [[ -e "$STAGE" ]]; then echo "stage kept"; fi
        ''')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        status = done.stdout.split("STAGE=", 1)[1].splitlines()[1:]
        self.assertEqual(sorted(status), sorted([" M scripts/plan_deck.py", " D README.md", "?? output/fix-summary.md",
                                                 "?? scripts/hosts", "?? scripts/new.py"]))
        self.assertNotIn("stage kept", done.stdout)
        # This checkout: as it was, but for the summary brought back for the pull request.
        self.assertEqual(git(self.tree, "status", "--porcelain", "-uall", "--", "scripts", "README.md", "state"), "")
        self.assertEqual((self.tree / "output" / "fix-summary.md").read_text(), "Fixed it\n")
        self.assertEqual((self.tree / "data" / "blogs.json").read_text(), "[]")

    def test_evidence_and_state_go_into_the_copy(self):
        (self.tree / "output" / "fix-verdict.json").write_text('{"verdict": "rejected"}')
        done = self.sh('''
            ci_sandbox_start && ci_sandbox_copy_in || exit 9
            echo '{"edited": true}' > "$SANDBOX_DIR/state/history.json"
            ci_sandbox_give output/fix-verdict.json output/not-there.json
            ci_sandbox_reset_state
            cat "$SANDBOX_DIR/output/fix-verdict.json" "$SANDBOX_DIR/state/history.json"
            ci_sandbox_run "$PY" -c "import os; print(os.path.samefile(os.getcwd(), os.environ['SANDBOX_DIR']))"
            ci_sandbox_stop
        ''')
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertEqual(done.stdout.splitlines()[-2:], ['{"verdict": "rejected"}{}', "True"])

    def test_no_sandbox_means_no_repair(self):
        done = self.sh('SELFHEAL_SANDBOX=auto; export SELFHEAL_SANDBOX; '
                       'if [[ "$(uname -s)" == Linux ]] && sudo -n true 2>/dev/null; then exit 0; fi; '
                       'ci_sandbox_start; echo "rc=$?"')
        if "rc=" in done.stdout:
            self.assertIn("rc=1", done.stdout)
            self.assertIn("cannot isolate the repair agent", done.stdout)


if __name__ == "__main__":
    unittest.main()
