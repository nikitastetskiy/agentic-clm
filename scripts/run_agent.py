#!/usr/bin/env python3
"""Run one of the repository's opencode agents from the command line (used by the pipeline scripts).

    python scripts/run_agent.py repo-fixer --model ica-gateway/claude-sonnet-5-5 \
        --message "The weekly build failed. Start with output/failure.json."

The agent works in the repository root. Tokens that only the publishing steps need are removed from
its environment, so nothing it runs can read them.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import agent  # noqa: E402
from lib.common import REPO_ROOT, log  # noqa: E402

PUBLISH_ONLY = ("GIT_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_TOKEN", "SELFHEAL_WEBHOOK_URL",
                "SELFHEAL_WEBHOOK_TOKEN", "IBMCLOUD_API_KEY", "BOB_API_KEY")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("agent")
    ap.add_argument("--model", required=True)
    ap.add_argument("--fallback-model", default="", help='tried when --model gives no answer at all ("none": no fallback)')
    ap.add_argument("--answered-file", help="the agent's answer: if it exits non-zero without writing this "
                                            "file, it gave no answer and the fallback model runs")
    ap.add_argument("--record", help="write the name of the model that ran last to this file")
    ap.add_argument("--message", required=True)
    ap.add_argument("--timeout", type=int, default=2700)
    ap.add_argument("--log", default=str(REPO_ROOT / "output" / "agent.log"))
    args = ap.parse_args()
    if not agent.available():
        log("ERROR: opencode is not installed")
        return 127
    def answered(code: int) -> bool:
        return code == 0 or not args.answered_file or (REPO_ROOT / args.answered_file).exists()

    code, model = agent.run_first(args.agent, args.message, models=[args.model, "" if args.fallback_model == "none" else args.fallback_model],
                                  answered=answered, cwd=REPO_ROOT, timeout=args.timeout,
                                  log_file=Path(args.log), drop_env=PUBLISH_ONLY)
    if args.record:
        Path(args.record).write_text(model + "\n", encoding="utf-8")
    log(f"agent {args.agent} on {model} exited {code}; transcript in {args.log}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
