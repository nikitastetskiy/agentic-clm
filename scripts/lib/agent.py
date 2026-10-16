"""Run an opencode agent non-interactively (the LLM steps of the pipeline).

Two agents live in .opencode/agents/:
  * deck-enrich — words new release notes / event abstracts for the deck (small model, weekly);
  * repo-fixer  — repairs the repository when a build fails (large model, self-heal pipeline).

Which model serves which is decided by the caller (ENRICH_MODEL / FIX_MODEL); nothing here names a
provider. Credentials are whatever opencode is configured with: the stored login on a laptop, or — in
a pipeline — the provider block that ci/lib.sh passes in OPENCODE_CONFIG_CONTENT, built from secrets.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from lib.common import REPO_ROOT, log

AGENT_DIR = REPO_ROOT / ".opencode"


def available() -> bool:
    return shutil.which("opencode") is not None


def run(agent: str, message: str, *, model: str, cwd: Path, timeout: int = 900,
        log_file: Path | None = None, drop_env: tuple[str, ...] = ()) -> int:
    """Run `opencode run --agent <agent>` in `cwd` and return its exit code (124 on timeout).

    `cwd` is the agent's whole world: it reads and edits files there and, with the agents' own
    ``external_directory: deny``, nowhere else. Agent definitions are loaded from the repository even
    when `cwd` is a scratch directory. `drop_env` names variables the agent's process must not
    inherit (tokens that only later pipeline steps need)."""
    env = {k: v for k, v in os.environ.items() if k not in drop_env}
    env["OPENCODE_CONFIG_DIR"] = str(AGENT_DIR)
    cmd = ["opencode", "run", "--agent", agent, "--model", model, "--dir", str(cwd), message]
    log(f"opencode: agent={agent} model={model} dir={cwd}")
    try:
        proc = subprocess.run(cmd, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=timeout)
        output, code = proc.stdout, proc.returncode
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
        output += f"\n[timeout after {timeout}s]"
        code = 124
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text(output, encoding="utf-8")
    return code
