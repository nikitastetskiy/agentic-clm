#!/usr/bin/env python3
"""Decide whether the repair agent's change may be proposed as a pull request.

The self-heal pipeline lets a model edit the repository. This script is the part of that pipeline the
model cannot argue with: it looks at what actually changed in the working tree and applies fixed
rules. It runs BEFORE the fix is tested, committed or pushed.

Rejected outright:
  * a change to the pipeline's own plumbing, the agent definitions, the template or this script
    (PROTECTED) — a repair has no business there, and an agent that was talked into it by something
    it read must not be able to widen its own permissions;
  * a deleted test;
  * a change larger than FIX_MAX_LINES (default 600) changed lines — a repair is small; anything
    bigger needs a person;
  * a change or fix summary that contains the value of one of the pipeline's secrets (lib/redact.py)
    — the agent works next to the gateway key, and nothing it writes may carry it out.

Allowed but flagged for the reviewer: any change to the gate (scripts/verify_build.py, the layout rules it
shares with the builder in scripts/lib/layout.py, tests/).
Ignored: data/, output/ and state/ — generated files are never part of a fix (the pipeline resets
state/ before it tests the change, so a build cannot be made to pass by editing its inputs).

Prints a JSON verdict and exits 0 (ok), 3 (the agent reported NO-FIX, or changed nothing) or
4 (rejected).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import redact  # noqa: E402
from lib.common import REPO_ROOT  # noqa: E402

PROTECTED = ("ci/", ".opencode/", ".github/", "templates/", "opencode.json", "Dockerfile",
             "docker-compose.yml", ".gitignore", ".dockerignore", ".env", ".pipeline-config",
             "scripts/check_fix.py", "scripts/report_fix.py", "scripts/publish_release.py",
             "scripts/scrub_secrets.py", "scripts/lib/agent.py", "scripts/lib/github.py",
             "scripts/lib/redact.py")
GENERATED = ("data/", "output/", "state/")
GATE = ("scripts/verify_build.py", "scripts/lib/layout.py", "tests/")
SUMMARY = REPO_ROOT / "output" / "fix-summary.md"


def git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True,
                          check=True).stdout


def changed_files() -> list[tuple[str, str]]:
    """(status, path) for every tracked change and untracked file, generated paths excluded."""
    out = []
    for line in git("status", "--porcelain=v1", "-uall").splitlines():
        status, path = line[:2].strip(), line[3:].split(" -> ")[-1].strip('"')
        if not path.startswith(GENERATED):
            out.append((status, path))
    return out


def changed_lines(files: list[tuple[str, str]]) -> int:
    total = 0
    for line in git("diff", "--numstat", "HEAD").splitlines():
        added, removed, path = line.split("\t", 2)
        if not path.startswith(GENERATED) and added.isdigit():
            total += int(added) + int(removed)
    for status, path in files:
        if status == "??":
            try:
                total += len((REPO_ROOT / path).read_text(encoding="utf-8", errors="replace").splitlines())
            except OSError:
                pass
    return total


def changed_text(files: list[tuple[str, str]]) -> list[str]:
    """What the change adds: the diff of tracked files and the content of new ones."""
    texts = [git("diff", "HEAD", "--", ".", *(f":!{g}" for g in GENERATED))]
    for status, path in files:
        if status == "??":
            try:
                texts.append((REPO_ROOT / path).read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass
    return texts


def judge(files: list[tuple[str, str]], lines: int, summary: str, limit: int,
          leaks: list[str] = ()) -> dict:
    """The verdict on a change: `files` are (git status, path) pairs with generated paths already
    left out, `lines` the changed-line count, `summary` the agent's fix summary, `leaks` the names
    of the secrets whose value the change or the summary contains."""
    headline = summary.strip().splitlines()[0].strip() if summary.strip() else ""
    verdict = {"verdict": "ok", "headline": headline, "files": [p for _s, p in files], "lines": lines,
               "touches_gate": any(p.startswith(GATE) for _s, p in files), "reasons": []}
    if headline.upper().startswith("NO-FIX"):
        verdict["verdict"] = "no-fix"
        return verdict
    if not files:
        verdict["verdict"] = "no-change"
        verdict["reasons"].append("the agent changed no file")
        return verdict
    verdict["reasons"] += [f"protected path changed: {p}" for _s, p in files if p.startswith(PROTECTED)]
    verdict["reasons"] += [f"test deleted: {p}" for s, p in files if "D" in s and p.startswith("tests/")]
    verdict["reasons"] += [f"the change contains the value of the secret {name}" for name in leaks]
    if lines > limit:
        verdict["reasons"].append(f"{lines} changed lines exceeds the limit of {limit}")
    if not headline:
        verdict["reasons"].append("output/fix-summary.md is missing")
    if verdict["reasons"]:
        verdict["verdict"] = "rejected"
    return verdict


def main() -> int:
    files = changed_files()
    summary = SUMMARY.read_text(encoding="utf-8") if SUMMARY.exists() else ""
    leaks = redact.found_in([summary, *changed_text(files)]) if files or summary else []
    verdict = judge(files, changed_lines(files) if files else 0, summary,
                    int(os.environ.get("FIX_MAX_LINES", "600")), leaks)
    verdict["headline"] = redact.scrub(verdict["headline"])   # it becomes the commit message
    print(json.dumps(verdict, indent=2))
    return {"ok": 0, "no-fix": 3, "no-change": 3, "rejected": 4}[verdict["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
