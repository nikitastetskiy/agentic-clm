#!/usr/bin/env python3
"""Create a timestamped working copy of the original deck. The original is never modified.

Usage: make_working_copy.py [SOURCE.pptx]
Prints the path of the created copy to stdout (so run.sh can capture it).
"""
from __future__ import annotations

import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = REPO_ROOT / "output"


def find_original() -> Path:
    candidates = [
        p for p in REPO_ROOT.glob("*.pptx")
        if "output" not in p.parts and not p.name.startswith("~$")
    ]
    if not candidates:
        raise SystemExit("No source .pptx found in repo root.")
    if len(candidates) > 1:
        raise SystemExit(f"Multiple .pptx in repo root; pass one explicitly: {[p.name for p in candidates]}")
    return candidates[0]


def main() -> int:
    src = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else find_original()
    if not src.exists():
        raise SystemExit(f"Source not found: {src}")
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    safe_stem = src.stem.replace("  ", " ").strip()
    dest = OUTPUT_DIR / f"{safe_stem} — {stamp}.pptx"
    shutil.copy2(src, dest)
    print(dest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
