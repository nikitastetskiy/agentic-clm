#!/usr/bin/env python3
"""Remove the pipeline's secret values from files before they are kept with a run.

    python3 scripts/scrub_secrets.py output data

The workflows run this just before they save output/ and the reports as a run artifact: those files
are outside the CI's log masking, and in a self-heal run a model with a shell wrote some of them.
Every value of a secret in the environment (lib/redact.py) is replaced by "[<NAME> removed]". Prints
the files that held one (never the value) and exits 0.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import redact  # noqa: E402


def main(roots: list[str]) -> int:
    names = [name for name, _value in redact.secrets()]
    changed = [path for root in roots if Path(root).is_dir() for path in redact.scrub_tree(Path(root))]
    print(f"scrubbed {len(changed)} file(s) of {', '.join(names) or 'no secrets set'}")
    for path in changed:
        print(f"  {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["output"]))
