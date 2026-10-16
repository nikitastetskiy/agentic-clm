#!/usr/bin/env bash
#
# Weekly pipeline: build this week's deck, publish it to GitHub, keep the build state.
#
#   1. bash build.sh           fetch -> plan -> enrich (small model) -> build -> verify
#   2. on success              publish the deck as release deck-<date>; commit state/ back
#   3. on failure              hand over to the self-heal pipeline and fail this run
#
# A failed weekly run never publishes anything: last week's release stays the latest one until a
# verified deck replaces it.
#
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck source=ci/lib.sh
source ci/lib.sh
ci_setup
ci_preflight

ci_log "weekly build"
ENRICH="$(prop enrich auto)" bash build.sh
rc=$?

if [[ "$rc" -eq 0 ]]; then
  ci_log "publish"
  if ! ci_publish; then
    # The deck is built and verified; only the upload failed. That is not something a code fix
    # repairs, so fail loudly instead of calling the repair agent.
    ci_warn "publishing failed — the verified deck is in output/ of this run"
    exit 60
  fi
  ci_log "keep build state"
  ci_commit_state "chore(state): weekly build $(date +%Y-%m-%d)"
  exit 0
fi

step="$("$PY" -c 'import json; print(json.load(open("output/failure.json")).get("step", "unknown"))' 2>/dev/null || echo unknown)"
ci_log "build failed at '${step}' (exit ${rc}) — handing over to the self-heal pipeline"
ci_trigger_selfheal "$step" "$rc" || true
exit "$rc"
