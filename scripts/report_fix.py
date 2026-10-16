#!/usr/bin/env python3
"""Report the outcome of a self-heal run on GitHub: a pull request for a verified fix, or an issue
when the build could not be repaired.

    python scripts/report_fix.py --pull --branch selfheal/20261013-0612 [--base main]
    python scripts/report_fix.py --issue --reason "fix did not pass the tests"

The pull request carries the agent's own summary (output/fix-summary.md) plus what the pipeline
verified independently, and says so when the change touches the verification gate. It is never
merged automatically: a model wrote this code, a person decides whether it goes in.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.common import REPO_ROOT, log  # noqa: E402
from lib.github import GitHub, GitHubError  # noqa: E402

OUTPUT = REPO_ROOT / "output"


def read(name: str) -> str:
    path = OUTPUT / name
    return path.read_text(encoding="utf-8", errors="replace").strip() if path.exists() else ""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--pull", action="store_true")
    mode.add_argument("--issue", action="store_true")
    ap.add_argument("--branch", default="")
    ap.add_argument("--base", default="main")
    ap.add_argument("--reason", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    gh = GitHub.from_checkout(dry_run=args.dry_run)
    if not gh.token:
        log("ERROR: no GitHub token in the environment (GIT_TOKEN)")
        return 1
    summary = read("fix-summary.md")
    verdict = json.loads(read("fix-verdict.json") or "{}")
    failure = json.loads(read("failure.json") or "{}")
    run_url = os.environ.get("PIPELINE_RUN_URL", "")
    model = os.environ.get("FIX_MODEL", "")
    footer = "\n\n---\n" + "\n".join(filter(None, [
        f"Self-heal pipeline run: {run_url}" if run_url else "",
        f"Repair agent: `repo-fixer` on `{model}`" if model else "",
        "Checked by the pipeline, independently of the agent: change stays out of protected paths; "
        "`python -m unittest discover -s tests` passes; `bash build.sh` rebuilds and verifies the deck."
        if args.pull else "",
    ]))
    try:
        if args.pull:
            headline = (verdict.get("headline") or summary.splitlines()[0] if summary else "repair")[:90]
            warn = ("> **This change touches the verification gate** (`scripts/verify_build.py` or "
                    "`tests/`). Check that it does not simply relax a check.\n\n"
                    if verdict.get("touches_gate") else "")
            pull = gh.open_pull(args.branch, args.base, f"self-heal: {headline}",
                                f"{warn}{summary}{footer}\n\nNot merged automatically — please review.")
            log(f"pull request: {pull.get('html_url', '(dry run)')}")
        else:
            step = failure.get("step", "unknown step")
            tail = "\n".join(failure.get("log_tail", [])[-40:])
            body = (f"The weekly deck build failed at **{step}** (exit {failure.get('exit_code', '?')}) and "
                    f"the self-heal pipeline could not repair it.\n\n**Why no fix:** {args.reason or 'see below'}\n\n"
                    + (f"**Agent summary**\n\n{summary}\n\n" if summary else "")
                    + (f"**Rejected change**\n\n```json\n{json.dumps(verdict, indent=2)}\n```\n\n"
                       if verdict.get("verdict") == "rejected" else "")
                    + f"**Log tail**\n\n```\n{tail[-6000:]}\n```" + footer)
            issue = gh.open_issue(f"Weekly deck build failed: {step}", body, ["auto-pptx", "build-failure"])
            log(f"issue: {issue.get('html_url', '(dry run)')}")
    except GitHubError as exc:
        log(f"ERROR: {exc}")
        return 1
    for call in gh.calls:
        log(f"  [dry-run] {call}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
