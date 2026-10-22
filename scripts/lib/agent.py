"""Run an opencode agent non-interactively (the LLM steps of the pipeline).

Two agents live in .opencode/agents/:
  * deck-enrich — words new release notes / event abstracts for the deck (small model, weekly);
  * repo-fixer  — repairs the repository when a build fails (large model, self-heal pipeline).

Which model serves which is decided by the caller (ENRICH_MODEL / FIX_MODEL, each with a fallback of
another vendor's that run_first() turns to when the first gives no answer, then Bob as the last
resort); nothing here names a provider. A `bob/...` model runs the same agent's instructions through
Bob Shell (lib/bob.py), with the tools the agent's own permissions allow. Credentials are whatever opencode is configured with: the stored login on a laptop, or — in
a pipeline — the provider block that ci/lib.sh passes in OPENCODE_CONFIG_CONTENT, built from secrets.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import yaml

from lib import bob, redact
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
        # The transcript is kept with the run, outside the CI's log masking: no secret value in it.
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text(redact.scrub(output), encoding="utf-8")
    return code


def run_bob(agent: str, message: str, *, cwd: Path, timeout: int = 900, log_file: Path | None = None) -> int:
    """The agent's instructions and `message`, run by Bob Shell in `cwd`; returns 0 when Bob finished.
    Bob gets the tools the agent's opencode permissions allow: reading, editing when `edit` is allowed,
    and running the commands in bob.REPAIR_COMMANDS when `bash` is."""
    text = (AGENT_DIR / "agents" / f"{agent}.md").read_text(encoding="utf-8")
    _, front, body = text.split("---", 2)
    allowed = yaml.safe_load(front).get("permission") or {}
    tools = ("read",) + (("edit",) if allowed.get("edit") == "allow" else ()) \
        + (("execute",) if isinstance(allowed.get("bash"), dict) or allowed.get("bash") == "allow" else ())
    log(f"bob: agent={agent} tools={','.join(tools)} dir={cwd}")
    status, said, stats = bob.run(f"{body.strip()}\n\n---\n\n{message}", workspace=cwd, mode="agent", tools=tools,
                                  commands=bob.REPAIR_COMMANDS if "execute" in tools else (), max_turns=120,
                                  max_cost=float(os.environ.get("BOB_MAX_COST_AGENT") or 5), timeout=timeout)
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text(redact.scrub(f"Bob Shell, agent {agent}: {'finished' if status == 200 else 'failed'}\n"
                                         f"stats: {stats}\n\n{said}\n"), encoding="utf-8")
    return 0 if status == 200 else 1


def run_first(agent: str, message: str, *, models: list[str], answered, cwd: Path, timeout: int = 900,
              log_file: Path | None = None, drop_env: tuple[str, ...] = ()) -> tuple[int, str]:
    """Run the agent on each of `models` in turn until `answered(exit code)` says one gave its answer;
    returns (exit code, the model that ran last). Only a model that gives no answer at all — refused by
    the gateway, unreachable, out of time — is a reason to try the next: an answer the pipeline then
    rejects is an answer. Each model after the first keeps its own transcript (`<log>-2`, ...)."""
    code, used = 127, ""
    for n, model in enumerate(dict.fromkeys(m for m in models if m)):
        if n:
            log(f"{agent}: {used} gave no answer (exit {code}); trying {model}")
        transcript = log_file if not n or log_file is None else log_file.with_name(f"{log_file.stem}-{n + 1}{log_file.suffix}")
        if model.startswith("bob/"):   # Bob Shell keeps every secret but its own key from Bob itself
            code, used = run_bob(agent, message, cwd=cwd, timeout=timeout, log_file=transcript), model
        else:
            code, used = run(agent, message, model=model, cwd=cwd, timeout=timeout, log_file=transcript,
                             drop_env=drop_env), model
        if answered(code):
            break
    return code, used
