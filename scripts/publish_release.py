#!/usr/bin/env python3
"""Publish a built deck to GitHub as a release — the weekly artifact.

    python scripts/publish_release.py            # release deck-<build date> with the deck attached
    python scripts/publish_release.py --check    # read-only: can this token see the repo's releases?
    python scripts/publish_release.py --dry-run  # print the API calls instead of making them

Each weekly build becomes one release in the repository the checkout came from (github.ibm.com or
github.com): tag ``deck-<YYYY-MM-DD>``, the build summary as its notes, and as assets the .pptx, the
summary, the build and verification reports and, when one was rendered, a PDF preview. The newest
release is what ``<repo>/releases/latest`` points at, so that link always opens this week's deck.

Releases are used rather than commits because a deck is a 7 MB binary: committed weekly it would
bloat every clone, while release assets live outside the git history and can be pruned (--keep).

Publishing the same build date again updates that release in place. The token is read from GIT_TOKEN
(a pipeline secret); it needs write access to the repository's contents. A dry run needs no token.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import redact  # noqa: E402
from lib.common import DATA_DIR, REPO_ROOT, log, read_json  # noqa: E402
from lib.github import GitHub, GitHubError  # noqa: E402

TAG_PREFIX = "deck-"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--check", action="store_true", help="only verify access (read-only)")
    ap.add_argument("--dry-run", action="store_true", help="print the write calls instead of making them")
    ap.add_argument("--keep", type=int, default=0, help="after publishing, delete all but the newest N "
                    "deck releases (0 = keep everything)")
    ap.add_argument("--note", default="", help="a line put at the top of the release notes")
    ap.add_argument("--extra", action="append", default=[], help="additional file to attach (repeatable)")
    args = ap.parse_args()

    gh = GitHub.from_checkout(dry_run=args.dry_run)
    # A dry run sends nothing (lib/github.py), so it needs no token; a check is a test of the token.
    if not gh.token and (args.check or not args.dry_run):
        log("ERROR: no GitHub token in the environment (GIT_TOKEN)")
        return 1
    try:
        if args.check:
            decks = [r["tag_name"] for r in gh.list_releases() if r["tag_name"].startswith(TAG_PREFIX)]
            log(f"access ok: {gh.web_url()} — {len(decks)} deck release(s)"
                + (f", newest {decks[0]}" if decks else ""))
            return 0

        build = read_json("build-report.json")
        verify = read_json("verify-report.json")
        if not build or not verify.get("ok"):
            log("ERROR: no verified build to publish (data/build-report.json + a passing verify-report.json)")
            return 1
        deck = Path(build["deck"])
        day = build["build"]["date"]
        summary = REPO_ROOT / "output" / f"summary-{day}.md"
        assets = [deck, summary, DATA_DIR / "build-report.json", DATA_DIR / "verify-report.json"]
        assets += [Path(p) for p in args.extra]
        missing = [str(a) for a in assets[:2] if not a.exists()]
        if missing:
            log(f"ERROR: missing {missing}")
            return 1
        notes = summary.read_text(encoding="utf-8")
        if args.note:
            notes = f"> {args.note}\n\n{notes}"
        # A release is read by everyone who can see the repository: nothing that carries a secret's value
        # is published, inside a deck's compressed parts included.
        leaked = sorted({name for asset in assets if asset.exists() for name in redact.found_in_file(asset)}
                        | set(redact.found_in([notes])))
        if leaked:
            log(f"ERROR: not published: the release would carry the value of {', '.join(leaked)}")
            return 1
        sha = subprocess.run(["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
                             capture_output=True, text=True, check=True).stdout.strip()
        tag = f"{TAG_PREFIX}{day}"
        release = gh.ensure_release(tag, f"Product updates deck — {day}", notes, sha)
        for asset in assets:
            if asset.exists():
                gh.upload_asset(release, asset)
                log(f"  {'would attach' if args.dry_run else 'attached'} {asset.name}")
        for name in gh.prune_assets(release, {a.name for a in assets if a.exists()}):
            log(f"  removed {name} (left from an earlier build of this date)")
        log(f"{'would publish' if args.dry_run else 'published'} {gh.web_url(f'/releases/tag/{tag}')}")

        if args.keep > 0:
            decks = sorted((r for r in gh.list_releases() if r["tag_name"].startswith(TAG_PREFIX)),
                           key=lambda r: r["tag_name"], reverse=True)
            for old in decks[args.keep:]:
                gh.delete_release(old)
                log(f"  pruned {old['tag_name']}")
    except GitHubError as exc:
        log(f"ERROR: {exc}")
        return 1
    for call in gh.calls:
        log(f"  [dry-run] {call}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
