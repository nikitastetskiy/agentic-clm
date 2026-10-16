#!/usr/bin/env bash
#
# Self-heal pipeline: the weekly build failed — repair the repository with a LARGE model, prove the
# repair, propose it, and still deliver this week's deck.
#
#   1. reproduce       run the build again. If it passes now the failure was transient: publish and stop.
#   2. repair          the `repo-fixer` agent (FIX_MODEL, e.g. Claude Opus 5.5) diagnoses and edits the
#                      code. It cannot push, cannot see the GitHub token, and its edits to generated
#                      files are discarded.
#   3. guard           scripts/check_fix.py: fixed rules on what a repair may touch and how big it may be.
#   4. prove           the test suite and a complete rebuild + verification, run by this script — the
#                      agent's own claim that it works is not evidence.
#   5. propose         push a branch and open a pull request (never merged automatically); publish the
#                      deck built from the repaired code, marked as such.
#   else               open an issue with the failure and what the agent found. Exit non-zero.
#
# FIX_ATTEMPTS (default 2): a repair that fails step 3 or 4 gets one more round with that evidence.
#
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck source=ci/lib.sh
source ci/lib.sh
ci_setup
ci_preflight
ATTEMPTS="$(prop fix-attempts 2)"

# This script lets an agent edit the checkout and then commits what it finds there. On a checkout
# with someone's uncommitted work that would sweep their changes into the "repair" — refuse.
if [[ -n "$(git status --porcelain --untracked-files=all -- . ':!state' ':!data' ':!output')" ]]; then
  echo "refusing to run: the working tree has uncommitted changes (self-heal needs a clean checkout)" >&2
  exit 2
fi

# --- 1. reproduce -------------------------------------------------------------------------
ci_log "reproduce the failure"
ENRICH="$(prop enrich auto)" bash build.sh
rc=$?
if [[ "$rc" -eq 0 ]]; then
  ci_log "the build passes now — the earlier failure was transient; nothing to repair"
  ci_publish "Built by the self-heal pipeline after a transient failure of the weekly run." || exit 60
  ci_commit_state "chore(state): weekly build $(date +%Y-%m-%d)"
  exit 0
fi
if ! command -v opencode >/dev/null 2>&1; then
  ci_warn "opencode is not installed in this image — cannot run the repair agent"
  "$PY" scripts/report_fix.py --issue --reason "the self-heal runner has no opencode CLI" ${DRY[@]+"${DRY[@]}"} || true
  exit "$rc"
fi

BRANCH="selfheal/$(date +%Y%m%d-%H%M)"
reason="the repair agent did not produce a change"
for attempt in $(seq 1 "$ATTEMPTS"); do
  # --- 2. repair --------------------------------------------------------------------------
  ci_log "repair attempt ${attempt}/${ATTEMPTS} with ${FIX_MODEL}"
  rm -f output/fix-summary.md
  message="The weekly deck build failed. Start with output/failure.json, find the root cause, fix it, prove the fix, and write output/fix-summary.md."
  if [[ "$attempt" -gt 1 ]]; then
    message="${message} Your previous attempt was not accepted: ${reason}. The evidence is in output/fix-verdict.json, output/selfheal-tests.log and output/failure.json. If your earlier change is still in the working tree, correct it; if it is gone, it was set aside for breaking a rule."
  fi
  "$PY" scripts/run_agent.py repo-fixer --model "$FIX_MODEL" --message "$message" \
      --timeout "$(prop fix-timeout 2700)" --log "output/agent-attempt-${attempt}.log" || true

  # --- 3. guard ---------------------------------------------------------------------------
  git checkout -q -- state/ 2>/dev/null || true     # generated inputs are never part of a repair
  "$PY" scripts/check_fix.py > output/fix-verdict.json
  verdict=$?
  cat output/fix-verdict.json
  if [[ "$verdict" -eq 3 ]]; then
    reason="$(head -1 output/fix-summary.md 2>/dev/null || echo 'the agent changed nothing')"
    ci_log "no repair: ${reason}"
    break
  fi
  if [[ "$verdict" -ne 0 ]]; then
    reason="the change was rejected by the guard: $("$PY" -c 'import json; print("; ".join(json.load(open("output/fix-verdict.json"))["reasons"]))')"
    # Set the rejected change aside (kept in the stash for inspection) so the next round starts clean.
    git stash push -q -u -m "rejected self-heal attempt ${attempt}" -- . ':!state' ':!data' ':!output' || true
    continue
  fi

  # --- 4. prove ---------------------------------------------------------------------------
  ci_log "prove the repair: tests"
  if ! "$PY" -m unittest discover -s tests > output/selfheal-tests.log 2>&1; then
    tail -30 output/selfheal-tests.log
    reason="the test suite fails with the change (output/selfheal-tests.log)"
    continue
  fi
  ci_log "prove the repair: full rebuild"
  if ! ENRICH="$(prop enrich auto)" bash build.sh; then
    reason="the build still fails with the change (output/failure.json)"
    continue
  fi

  # --- 5. propose -------------------------------------------------------------------------
  ci_log "propose the repair on ${BRANCH}"
  if [[ "${CI_DRY_RUN:-0}" == "1" ]]; then
    # A rehearsal leaves the verified change in the working tree for inspection: no branch, no commit.
    echo "  [dry-run] would commit to ${BRANCH}:"
    git status --short -- . ':!state' ':!data' ':!output' | sed 's/^/    /'
  else
    git checkout -q -b "$BRANCH"
    git add -A -- . ':!state' ':!data' ':!output'
    git commit -q -m "self-heal: $("$PY" -c 'import json; print(json.load(open("output/fix-verdict.json"))["headline"][:68] or "repair the weekly build")')" \
                  -m "Proposed by the auto-pptx self-heal pipeline (repo-fixer on ${FIX_MODEL}). Tests and a full rebuild were verified by the pipeline before this commit."
  fi
  if ci_git_push "HEAD:refs/heads/${BRANCH}" \
     && "$PY" scripts/report_fix.py --pull --branch "$BRANCH" --base "$BASE_BRANCH" ${DRY[@]+"${DRY[@]}"}; then
    ci_publish "Built from the UNMERGED repair branch \`${BRANCH}\` after the weekly run failed — see the open self-heal pull request." || exit 60
    exit 0
  fi
  reason="the repair is verified but could not be pushed or proposed (check the git-token's permissions)"
  break
done

ci_log "not repaired: ${reason}"
"$PY" scripts/report_fix.py --issue --reason "$reason" ${DRY[@]+"${DRY[@]}"} || true
exit 70
