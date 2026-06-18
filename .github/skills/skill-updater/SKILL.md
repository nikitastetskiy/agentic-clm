---
name: skill-updater
description: >-
  Checks whether vendored third-party skills (especially the Anthropic `pptx` skill) are out of
  date versus their upstream source before they are used, and can re-vendor them on request. Use
  this skill at the START of any deck-automation run, or whenever the user asks to update, refresh,
  check, or pin skills. Reads .github/skills/.skill-manifest.json.
user-invocable: true
---

# Skill Updater

Keeps vendored skills current and reproducible. The pptx skill is vendored at a pinned commit SHA
recorded in `.github/skills/.skill-manifest.json`. This skill compares that pin against upstream
and, depending on `update_policy`, notifies or re-vendors.

## When to run

- **Before every pipeline run**, as the first step (so the deck is always edited with a known-good
  pptx skill). `run.sh` calls `scripts/check_updates.py` automatically.
- On demand: "check skills", "update the pptx skill", "pin skills to latest".

## Usage

```bash
# Check only — exit 0 if current, exit 10 if an update is available (non-fatal):
python .github/skills/skill-updater/scripts/check_updates.py --check

# Re-vendor any out-of-date skills and bump the manifest SHA:
python .github/skills/skill-updater/scripts/check_updates.py --update
```

Authentication: uses `GITHUB_TOKEN` if set (recommended in CI/Docker to avoid rate limits);
otherwise falls back to unauthenticated GitHub REST API.

## update_policy (per skill in the manifest)

| Policy   | Behavior on a detected upstream change                                  |
|----------|-------------------------------------------------------------------------|
| `notify` | Report that an update exists; do NOT change files. (default, safe)      |
| `auto`   | Re-vendor the skill files and update `pinned_sha` automatically.        |
| `pin`    | Never check/update; treat the pinned SHA as frozen.                     |

## Behavior contract

1. Never silently change a skill's behavior mid-run. With `notify`, only print a message and set a
   non-zero advisory exit code (10) — the run continues with the current pinned version.
2. After an `--update`, re-verify file integrity and record the new SHA + timestamp in the manifest.
3. Treat the vendored pptx skill `LICENSE.txt` as authoritative; preserve it on re-vendor.
4. If upstream is unreachable, proceed with the pinned version and warn (do not fail the run).
