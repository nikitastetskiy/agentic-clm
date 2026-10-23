"""IBM Bob through Bob Shell 2, its official command-line client: the last fallback of every model step
(the wording, the pull request review, the repair agent) when the ICA gateway does not answer.

Bob's inference API turns other clients away at its firewall (Cloudflare answers HTTP 403 before the
key is looked at), so the pipeline never calls it directly: it runs `bob run`, headless.

Bob Shell is installed on demand, the way IBM's installer (install-bobshell.sh) does it: the package of
the pinned VERSION from IBM's download bucket, checked against the pinned SHA256, installed with npm
(Node 22.15 or later). Runs that never need Bob pay nothing for it. BOB_SHELL_INSTALL=0 turns that off
(the tests do).

Each run gets a home of its own whose settings turn auto-update and telemetry off and say which tools
Bob may use. In a headless run nothing asks: a tool that is not approved is not run. Bob never gets the
tool that runs commands: Bob's key cannot be kept from a command Bob runs (its firewall admits Bob Shell
alone, so the key cannot stay behind a relay as the gateway's does, scripts/key_relay.py). As the repair
agent Bob reads and edits; the pipeline runs the tests itself. The process sees BOB_API_KEY and no other
variable named like a credential, the pipeline's or anyone else's. In the self-heal sandbox
(lib/sandbox.py) Bob runs as the sandbox user, alone, and reads its key from its standard input.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

from lib import redact, sandbox

VERSION = "2.0.6"
SHA256 = "b3e75ad93f2b5236a4b60f1d008c3d6c12bd2748e81e210b80be302a3db0b5bd"
BUCKET = "https://s3.us-south.cloud-object-storage.appdomain.cloud/bob-shell"
TOOL_GROUPS = ("read", "edit", "execute", "browser", "mcp", "skill", "todo", "artifact", "subagent", "mode")
ALLOWED_TOOLS = ("read", "edit")   # never "execute": no command may run next to Bob's key
# Bob in the sandbox: the key arrives as the first line of the standard input, the prompt after it.
WITH_KEY_ON_STDIN = 'IFS= read -r BOB_API_KEY && export BOB_API_KEY && exec "$@"'


class BobError(RuntimeError):
    pass


def install_dir() -> Path:
    return Path(os.environ.get("BOB_SHELL_HOME") or Path.home() / ".cache" / "auto-pptx" / f"bobshell-{VERSION}")


def executable() -> Path | None:
    """The installed Bob Shell, if any (BOB_SHELL names another one, e.g. for the tests)."""
    if os.environ.get("BOB_SHELL"):
        return Path(os.environ["BOB_SHELL"])
    path = install_dir() / "node_modules" / ".bin" / "bob"
    return path if path.exists() else None


def node_version() -> tuple[int, int] | None:
    if not shutil.which("node"):
        return None
    found = re.findall(r"\d+", subprocess.run(["node", "--version"], capture_output=True, text=True).stdout)
    return (int(found[0]), int(found[1])) if len(found) >= 2 else None


def install() -> Path:
    """Install the pinned Bob Shell into install_dir(); returns its executable. Raises BobError."""
    found = executable()
    if found:
        return found
    if os.environ.get("BOB_SHELL_INSTALL") == "0":
        raise BobError("Bob Shell is not installed, and installing it is turned off (BOB_SHELL_INSTALL=0)")
    node = node_version()
    if not node or node < (22, 15) or not shutil.which("npm"):
        raise BobError(f"Bob Shell needs Node 22.15 or later and npm; this machine has "
                       f"{'Node ' + '.'.join(map(str, node)) if node else 'no Node'}")
    package = f"bobshell-{VERSION}.tgz"
    with tempfile.TemporaryDirectory() as tmp:
        tgz = Path(tmp) / package
        req = urllib.request.Request(f"{BUCKET}/{package}", headers={"User-Agent": "auto-pptx-pipeline"})
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                tgz.write_bytes(resp.read())
        except OSError as exc:
            raise BobError(f"could not download {package}: {getattr(exc, 'reason', exc)}") from None
        got = hashlib.sha256(tgz.read_bytes()).hexdigest()
        if got != SHA256:
            raise BobError(f"{package} is not the package pinned (SHA-256 {got}, expected {SHA256})")
        target = install_dir()
        target.mkdir(parents=True, exist_ok=True)
        done = subprocess.run(["npm", "install", "--prefix", str(target), "--registry=https://registry.npmjs.org/",
                               "--no-audit", "--no-fund", "--loglevel=error", str(tgz)],
                              capture_output=True, text=True, timeout=900)
        if done.returncode != 0:
            raise BobError(f"npm could not install {package}: {done.stderr.strip()[-300:]}")
    found = executable()
    if not found:
        raise BobError(f"{package} installed, but no bob command came with it")
    return found


def settings(tools: tuple[str, ...]) -> dict:
    approval = {"autoApprovalEnabled": True, "outsideWorkspaceAllowed": False,
                "allowed_permissions": [t for t in tools if t in ALLOWED_TOOLS]}
    return {"bobShell": {"autoUpdate": "none"}, "telemetry": {"enabled": False, "excludePayload": True},
            "licenseConsent": True, "approval": approval}


def run(prompt: str, *, workspace: Path, mode: str = "ask", tools: tuple[str, ...] = (), max_turns: int = 4,
        max_cost: float = 1.0, timeout: int = 600, env=None) -> tuple[int | None, str, dict]:
    """Run Bob Shell headless on `prompt` in `workspace` with only `tools` (tool groups among
    ALLOWED_TOOLS) enabled. Returns (200, Bob's last message, its stats), or (None, what went wrong, {})."""
    env = os.environ if env is None else env
    key = env.get("BOB_API_KEY", "")
    if not key:
        return None, "no BOB_API_KEY set", {}
    try:
        bob = install()
    except BobError as exc:
        return None, str(exc), {}
    tools = tuple(t for t in tools if t in ALLOWED_TOOLS)
    off = [g for g in TOOL_GROUPS if g not in tools]
    cmd = [str(bob), "run", "--mode", mode, "-w", str(workspace), "--max-turns", str(max_turns),
           "--max-cost", str(max_cost), "--accept-license", "--disable-mcp", "--disable-subagents",
           "--disable-tool-groups", ",".join(off), "-f", "json"]
    # In the sandbox an agent works on the sandbox's own copy of the repository, so Bob must run as that
    # user (lib/sandbox.py); a question needs no workspace of anyone's and runs as it is.
    boxed = sandbox.active(env) and mode == "agent"
    home = Path(tempfile.mkdtemp(prefix="bob-home-"))
    try:
        (home / ".bob").mkdir()
        (home / ".bob" / "settings.json").write_text(json.dumps(settings(tools)), encoding="utf-8")
        if boxed:
            sandbox.give(home)
            sandbox.clear()              # no other process of the sandbox user while Bob holds its key
            sandbox.unlink_links(workspace)
            done = sandbox.run(["/bin/bash", "-c", WITH_KEY_ON_STDIN, "bob", *cmd], cwd=workspace,
                               env=sandbox.environment(env) | {"HOME": str(home)}, input=f"{key}\n{prompt}",
                               capture_output=True, text=True, timeout=timeout)
        else:
            run_env = {k: v for k, v in env.items()
                       if k not in redact.SECRET_VARS and not redact.SECRET_NAME.search(k) and not k.startswith("BOBSHELL_")}
            run_env.update(HOME=str(home), BOB_API_KEY=key)
            done = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout, env=run_env,
                                  cwd=str(workspace))
    except subprocess.TimeoutExpired:
        return None, f"Bob Shell did not finish in {timeout}s", {}
    finally:
        if boxed:
            sandbox.clear()
            sandbox.take(home)
        shutil.rmtree(home, ignore_errors=True)
    lines = [line for line in done.stdout.splitlines() if line.strip().startswith("{")]
    try:
        result = json.loads(lines[-1]) if lines else {}
    except ValueError:
        result = {}
    if result.get("status") == "success":
        return 200, str(result.get("last_message") or ""), result.get("stats") or {}
    why = result.get("error") or result.get("last_message") or done.stderr.strip()[-300:] or f"exit {done.returncode}"
    return None, " ".join(redact.scrub(f"Bob Shell: {result.get('status') or 'failed'}: {why}", env).split())[:300], {}
