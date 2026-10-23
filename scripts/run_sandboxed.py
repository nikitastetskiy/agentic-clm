#!/usr/bin/env python3
"""Run a command in the self-heal sandbox: as the sandbox user, without a secret (lib/sandbox.py).

    python scripts/run_sandboxed.py --dir WORKDIR -- python -m unittest discover -s tests

ci/self-heal.sh proves a repair this way: the tests and the rebuild are code the agent wrote. Without a
sandbox (no SANDBOX_USER) the command runs as it is, in WORKDIR. Exits with the command's exit code.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import sandbox  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dir", required=True)
    ap.add_argument("command", nargs=argparse.REMAINDER)
    args = ap.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        ap.error("no command")
    sys.stdout.flush()
    return sandbox.run(command, cwd=Path(args.dir)).returncode


if __name__ == "__main__":
    raise SystemExit(main())
