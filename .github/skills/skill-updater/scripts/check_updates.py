#!/usr/bin/env python3
"""Check (and optionally re-vendor) vendored skills against their upstream source.

Comparison key = the SHA of the most recent commit that touched the skill's upstream path.
This changes iff the skill's content changes upstream, giving precise drift detection.

Exit codes:
  0  all skills current (or unreachable upstream -> warn + proceed)
  10 at least one update is available (advisory; non-fatal for `--check`)
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import tarfile
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
SKILLS_DIR = REPO_ROOT / ".github" / "skills"
MANIFEST = SKILLS_DIR / ".skill-manifest.json"
API = "https://api.github.com"


def gh_request(url: str, *, accept: str = "application/vnd.github+json") -> bytes:
    headers = {"User-Agent": "auto-pptx-skill-updater", "Accept": accept}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def parse_source(source: str) -> tuple[str, str]:
    # "github:owner/repo" -> ("owner", "repo")
    assert source.startswith("github:"), f"unsupported source: {source}"
    owner, repo = source[len("github:"):].split("/", 1)
    return owner, repo


def latest_path_sha(owner: str, repo: str, path: str, ref: str) -> str:
    url = f"{API}/repos/{owner}/{repo}/commits?path={path}&sha={ref}&per_page=1"
    data = json.loads(gh_request(url))
    if not data:
        raise RuntimeError(f"no commits found for {path}")
    return data[0]["sha"]


def revendor(owner: str, repo: str, path: str, sha: str, dest: Path) -> None:
    url = f"{API}/repos/{owner}/{repo}/tarball/{sha}"
    blob = gh_request(url, accept="application/octet-stream")
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
        members = tf.getmembers()
        root = members[0].name.split("/", 1)[0]
        prefix = f"{root}/{path}/"
        tmp = dest.parent / (dest.name + ".new")
        if tmp.exists():
            shutil.rmtree(tmp)
        for m in members:
            if not m.name.startswith(prefix) or m.isdir():
                continue
            rel = m.name[len(prefix):]
            target = tmp / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            f = tf.extractfile(m)
            if f is None:
                continue
            target.write_bytes(f.read())
        if dest.exists():
            shutil.rmtree(dest)
        tmp.rename(dest)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report only (default)")
    ap.add_argument("--update", action="store_true", help="re-vendor out-of-date skills")
    args = ap.parse_args()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    skills = manifest["skills"]
    update_available = False
    changed = False

    for name, meta in skills.items():
        policy = meta.get("update_policy", "notify")
        if policy == "pin":
            print(f"[skill-updater] {name}: pinned (skipped)")
            continue
        owner, repo = parse_source(meta["source"])
        path, ref, pinned = meta["path"], meta.get("ref", "main"), meta.get("pinned_sha")
        try:
            upstream = latest_path_sha(owner, repo, path, ref)
        except Exception as exc:  # noqa: BLE001
            print(f"[skill-updater] {name}: upstream unreachable ({exc}); using pinned {pinned[:12]}")
            continue

        if upstream == pinned:
            print(f"[skill-updater] {name}: up to date ({pinned[:12]})")
            continue

        update_available = True
        print(f"[skill-updater] {name}: UPDATE available {pinned[:12]} -> {upstream[:12]}")

        do_update = args.update and policy != "pin"
        if do_update:
            dest = SKILLS_DIR / name
            print(f"[skill-updater]   re-vendoring {name} @ {upstream[:12]} ...")
            revendor(owner, repo, path, upstream, dest)
            meta["pinned_sha"] = upstream
            meta["vendored_at"] = datetime.now(timezone.utc).isoformat()
            changed = True
            print(f"[skill-updater]   done.")
        elif policy == "notify":
            print(f"[skill-updater]   policy=notify: not changing files (run with --update to apply)")

    if changed:
        MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print("[skill-updater] manifest updated.")

    return 10 if (update_available and not changed) else 0


if __name__ == "__main__":
    raise SystemExit(main())
