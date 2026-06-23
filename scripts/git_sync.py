#!/usr/bin/env python3
"""Keep the shared "bot" repo in sync around a pipeline run.

Two subcommands, both BEST-EFFORT and NON-FATAL — a sync hiccup must never fail a deck run:

  pull     Pre-run. `git pull --rebase --autostash` from the upstream branch so this run picks up
           other users' newly merged learnings (corrections/). No-op when there's no upstream/remote
           or the remote is unreachable.

  propose  Post-run. If any SHAREABLE tracked file changed (the learning log under corrections/ by
           default), open a pull request with just those files and merge it when allowed:
             - parse host/owner/repo from `git remote get-url origin` (github.com or a GHE host);
             - require `gh` installed AND authenticated for that host (else: keep the change local);
             - require write/maintain/admin permission on the repo (else: no-op, leave it local);
             - branch learnings/<utc> -> commit only the shareable paths -> push -> `gh pr create`;
             - try `gh pr merge --squash --delete-branch`; if blocked, enable `--auto`; else leave open;
             - return to the original branch (fast-forwarding it when the PR merged immediately).

Env:
  GIT_SYNC=0          disable the pre-run pull
  AUTO_PR=0           disable the post-run PR
  AUTO_MERGE=0        open the PR but don't merge it (leave it for review)
  LEARNING_PR_BASE    base branch for the PR (default: the repo's default branch, usually main)
  LEARNING_PATHS      space/comma-separated pathspecs to share (default: "corrections/")
  GH_HOST / GH_ENTERPRISE_TOKEN   honoured by gh for enterprise hosts
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SHAREABLE = ["corrections/"]
CO_AUTHOR = "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"


def _say(msg: str) -> None:
    print(f"[git-sync] {msg}", flush=True)


def _env_off(name: str) -> bool:
    """True if env var `name` is explicitly set to a falsey value (0/false/no/off)."""
    return os.environ.get(name, "").strip().lower() in {"0", "false", "no", "off"}


def git(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=REPO_ROOT, text=True,
                          capture_output=True, check=check)


def gh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], cwd=REPO_ROOT, text=True, capture_output=True)


def _have(cmd: str) -> bool:
    from shutil import which
    return which(cmd) is not None


def _in_git_repo() -> bool:
    return git("rev-parse", "--is-inside-work-tree").returncode == 0


def _current_branch() -> str:
    return git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _upstream() -> str | None:
    cp = git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    return cp.stdout.strip() if cp.returncode == 0 and cp.stdout.strip() else None


def _remote_slug() -> tuple[str, str, str] | None:
    """(host, owner, repo) parsed from origin's URL, or None."""
    cp = git("remote", "get-url", "origin")
    if cp.returncode != 0:
        return None
    url = cp.stdout.strip()
    m = re.match(r"git@([^:]+):([^/]+)/(.+?)(?:\.git)?$", url) \
        or re.match(r"(?:https?|ssh)://(?:[^@]+@)?([^/]+)/([^/]+)/(.+?)(?:\.git)?$", url)
    if not m:
        return None
    return m.group(1), m.group(2), m.group(3)


# --------------------------------------------------------------------------------------- pull
def cmd_pull() -> int:
    if _env_off("GIT_SYNC"):
        _say("GIT_SYNC=0 — skipping pre-run pull")
        return 0
    if not _in_git_repo():
        _say("not a git repo — skipping pre-run pull")
        return 0
    up = _upstream()
    if not up:
        _say("no upstream branch tracked — skipping pre-run pull")
        return 0
    _say(f"pulling latest learnings: git pull --rebase --autostash ({up})")
    cp = git("pull", "--rebase", "--autostash")
    out = (cp.stdout + cp.stderr).strip()
    if cp.returncode == 0:
        last = out.splitlines()[-1] if out else "up to date"
        _say(f"pull ok — {last}")
    else:
        _say(f"pull skipped/failed (non-fatal): {out.splitlines()[-1] if out else cp.returncode}")
        # Never leave a half-finished rebase behind.
        if (REPO_ROOT / ".git" / "rebase-merge").exists() or (REPO_ROOT / ".git" / "rebase-apply").exists():
            git("rebase", "--abort")
            _say("aborted an interrupted rebase to keep the tree clean")
    return 0


# ------------------------------------------------------------------------------------ propose
def _shareable_paths() -> list[str]:
    raw = os.environ.get("LEARNING_PATHS", "").strip()
    if raw:
        return [p for p in re.split(r"[,\s]+", raw) if p]
    return list(DEFAULT_SHAREABLE)


def _has_changes(paths: list[str]) -> bool:
    cp = git("status", "--porcelain", "--", *paths)
    return bool(cp.stdout.strip())


def _default_base(host: str, owner: str, repo: str) -> str:
    env = os.environ.get("LEARNING_PR_BASE", "").strip()
    if env:
        return env
    cp = gh("repo", "view", f"{host}/{owner}/{repo}", "--json", "defaultBranchRef",
            "-q", ".defaultBranchRef.name")
    if cp.returncode == 0 and cp.stdout.strip():
        return cp.stdout.strip()
    up = _upstream()
    return up.split("/", 1)[1] if up and "/" in up else "main"


def cmd_propose() -> int:
    if _env_off("AUTO_PR"):
        _say("AUTO_PR=0 — not opening a learning PR")
        return 0
    if not _in_git_repo():
        return 0
    paths = _shareable_paths()
    if not _has_changes(paths):
        _say(f"no new learnings to share ({', '.join(paths)} unchanged)")
        return 0
    slug = _remote_slug()
    if not slug:
        _say("no parseable origin remote — learning kept local")
        return 0
    host, owner, repo = slug
    if not _have("gh"):
        _say("gh CLI not installed — learning kept local (commit & push it manually to share)")
        return 0
    if gh("auth", "status", "--hostname", host).returncode != 0:
        _say(f"gh not authenticated for {host} — learning kept local "
             f"(run: gh auth login -h {host}). Nothing pushed.")
        return 0
    perm = gh("repo", "view", f"{host}/{owner}/{repo}", "--json", "viewerPermission",
              "-q", ".viewerPermission")
    level = perm.stdout.strip().upper() if perm.returncode == 0 else ""
    if level not in {"WRITE", "MAINTAIN", "ADMIN"}:
        _say(f"no write access to {owner}/{repo} (permission={level or 'unknown'}) — "
             f"learning kept local; ask a maintainer to merge corrections/")
        return 0

    base = _default_base(host, owner, repo)
    start = _current_branch()
    if start in ("", "HEAD"):
        _say("detached HEAD — learning kept local (checkout a branch to share it)")
        return 0
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    branch = f"learnings/{stamp}"
    _say(f"sharing learnings -> PR into {base} (branch {branch})")

    if git("switch", "-c", branch).returncode != 0:
        _say("could not create the learnings branch — learning kept local")
        return 0
    msg = (f"learnings: sync corrections from a pipeline run ({stamp} UTC)\n\n"
           f"Automated learning sync: propagates new rules recorded in corrections/ so the\n"
           f"next run (for every user of this bot) applies them.\n\n{CO_AUTHOR}")
    commit = git("commit", "-m", msg, "--", *paths)
    if commit.returncode != 0:
        _say("nothing committed (paths had no tracked changes) — restoring and aborting")
        git("switch", start)
        git("branch", "-D", branch)
        return 0

    push = git("push", "-u", "origin", branch)
    if push.returncode != 0:
        tail = (push.stderr or push.stdout).strip().splitlines()
        _say(f"push failed (non-fatal): {tail[-1] if tail else push.returncode}")
        _restore_then_back(start, branch, paths)
        return 0

    pr = gh("pr", "create", "--repo", f"{host}/{owner}/{repo}", "--base", base,
            "--head", branch, "--title", f"learnings: sync corrections ({stamp} UTC)",
            "--body", "Automated learning sync from a pipeline run. Updates the shared "
                      "`corrections/` learning log so every user's next run applies the new rules.")
    url = pr.stdout.strip().splitlines()[-1] if pr.stdout.strip() else ""
    if pr.returncode != 0:
        _say(f"PR create failed (non-fatal): {(pr.stderr or pr.stdout).strip()}")
        git("switch", start)
        return 0
    _say(f"opened PR: {url}")

    if _env_off("AUTO_MERGE"):
        _say("AUTO_MERGE=0 — PR left open for review")
        git("switch", start)
        return 0

    merged = _try_merge(host, owner, repo, url or branch)
    git("switch", start)
    if merged:
        git("branch", "-D", branch)  # local cleanup; remote branch was deleted by --delete-branch
        # Bring the squash-merge commit into the local base branch.
        if _current_branch() == base:
            git("pull", "--rebase", "--autostash")
        _say("learning merged and synced locally ✓")
    else:
        _say(f"PR is open but not merged automatically — review/merge it: {url}")
    return 0


def _try_merge(host: str, owner: str, repo: str, selector: str) -> bool:
    repo_arg = ["--repo", f"{host}/{owner}/{repo}"]
    now = gh("pr", "merge", selector, *repo_arg, "--squash", "--delete-branch")
    if now.returncode == 0:
        return True
    # Branch protection may require checks/review: fall back to "merge when ready".
    auto = gh("pr", "merge", selector, *repo_arg, "--squash", "--auto", "--delete-branch")
    if auto.returncode == 0:
        _say("enabled auto-merge (will merge once required checks/reviews pass)")
    return False


def _restore_then_back(start: str, branch: str, paths: list[str]) -> None:
    """After a failed push: return to `start` with the shareable files restored (nothing lost)."""
    git("switch", start)
    git("restore", "--source", branch, "--worktree", "--", *paths)
    git("branch", "-D", branch)
    _say("restored the learning to your working tree (uncommitted) — nothing was lost")


def main() -> int:
    ap = argparse.ArgumentParser(description="Sync the shared bot repo around a run (best-effort).")
    ap.add_argument("command", choices=["pull", "propose"])
    args = ap.parse_args()
    try:
        return cmd_pull() if args.command == "pull" else cmd_propose()
    except Exception as exc:  # never let a sync error fail the pipeline
        _say(f"unexpected error (non-fatal): {exc}")
        return 0


if __name__ == "__main__":
    sys.exit(main())
