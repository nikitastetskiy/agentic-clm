#!/usr/bin/env bash
#
# auto-pptx pipeline orchestrator.
#
# Phases:
#   0. skill-updater  — ensure the vendored pptx skill matches its pin (advisory)
#   1. fetch          — deterministic Python: releases + blogs + events-prep
#   2. copy           — timestamped working copy (original is never touched)
#   3. data (agent)   — deck-data: web_fetch events/TFE + changelog highlights
#   3b. release edit   — deterministic Python: apply shift/refresh + date-reconcile to slides 7,8,11,12
#   4. build (agent)  — pptx-orchestrator: edit fallback/blog slides, hide empty ones, repack
#   5. review (agent) — deck-reviewer: verify style/data, record learnings
#
# Env (see .env.example):
#   COPILOT_MODEL          model override (optional)
#   SKILL_UPDATE_POLICY    check | update   (default: check)
#   SKIP_FETCH/SKIP_DATA/SKIP_BUILD/SKIP_REVIEW = 1 to skip a phase
#   SOURCE_PPTX            explicit source deck (default: the single .pptx in repo root)
#
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# Prefer the project venv (provides `python`/`python3` with pptx-skill deps) for this run AND for
# the nested Copilot agent subprocesses, which inherit this environment.
if [[ -d "$REPO_ROOT/.venv/bin" ]]; then
  export PATH="$REPO_ROOT/.venv/bin:$PATH"
fi

# --- load .env if present (KEY=VALUE lines) -------------------------------------------
if [[ -f .env ]]; then
  set -a; # shellcheck disable=SC1091
  source .env; set +a
fi

PY="${PYTHON:-python3}"
log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m[warn] %s\033[0m\n' "$*"; }

# Resolve the model for a phase: per-phase override wins, else global COPILOT_MODEL, else default.
#   deck-data -> COPILOT_MODEL_DATA, pptx-orchestrator -> COPILOT_MODEL_BUILD, deck-reviewer -> COPILOT_MODEL_REVIEW
phase_model() {
  case "$1" in
    deck-data)         echo "${COPILOT_MODEL_DATA:-${COPILOT_MODEL:-}}" ;;
    pptx-orchestrator) echo "${COPILOT_MODEL_BUILD:-${COPILOT_MODEL:-}}" ;;
    deck-reviewer)     echo "${COPILOT_MODEL_REVIEW:-${COPILOT_MODEL:-}}" ;;
    *)                 echo "${COPILOT_MODEL:-}" ;;
  esac
}

run_copilot() {
  # run_copilot <agent> <prompt>
  local agent="$1"; local prompt="$2"
  local model_args=() model; model="$(phase_model "$agent")"
  [[ -n "$model" ]] && model_args=(--model "$model")
  log "Copilot phase: agent=$agent${model:+ (model=$model)}"
  local out_log="output/phase-${agent}.log"
  set +e
  copilot --agent "$agent" \
          --allow-all \
          --no-ask-user \
          --no-color \
          --log-level error \
          "${model_args[@]}" \
          -p "$prompt" 2>&1 | tee "$out_log"
  local rc="${PIPESTATUS[0]}"
  set -e
  if grep -qiE 'quota_exceeded|exceeded your monthly quota' "$out_log"; then
    printf '\n\033[1;31m[blocked] Copilot quota exceeded during agent "%s".\033[0m\n' "$agent"
    echo "  The deterministic phases (skill check, fetch, working copy) completed."
    echo "  Provide credentials with available quota (e.g. COPILOT_GITHUB_TOKEN in .env) and re-run,"
    echo "  or wait for your monthly quota to reset. See output/phase-${agent}.log."
    exit 7
  fi
  if grep -qiE '\b401\b|unauthor|authentication' "$out_log"; then
    printf '\n\033[1;31m[blocked] Copilot authentication failed for agent "%s".\033[0m\n' "$agent"
    echo "  Set COPILOT_GITHUB_TOKEN (FG-PAT with \"Copilot Requests\") in .env. See output/phase-${agent}.log."
    exit 8
  fi
  if [[ "$rc" -ne 0 ]]; then
    warn "agent $agent exited with code $rc (see $out_log)"
    exit "$rc"
  fi
}

# --- Phase 0: skill-updater ------------------------------------------------------------
POLICY="${SKILL_UPDATE_POLICY:-check}"
log "Phase 0: skill-updater ($POLICY)"
if [[ "$POLICY" == "update" ]]; then
  "$PY" .github/skills/skill-updater/scripts/check_updates.py --update || true
else
  "$PY" .github/skills/skill-updater/scripts/check_updates.py --check || \
    warn "pptx skill update available (run with SKILL_UPDATE_POLICY=update to apply)"
fi

# --- Phase 1: deterministic fetchers (run in parallel; they write independent files) ---
if [[ "${SKIP_FETCH:-0}" != "1" ]]; then
  log "Phase 1: fetch (releases, blogs, events-prep) [parallel]"
  mkdir -p output
  "$PY" scripts/fetch_releases.py > output/fetch-releases.log 2>&1 & pid_rel=$!
  "$PY" scripts/fetch_blogs.py    > output/fetch-blogs.log    2>&1 & pid_blog=$!
  "$PY" scripts/fetch_events.py   > output/fetch-events.log   2>&1 & pid_evt=$!
  rc_rel=0; rc_blog=0; rc_evt=0
  wait "$pid_rel"  || rc_rel=$?
  wait "$pid_blog" || rc_blog=$?
  wait "$pid_evt"  || rc_evt=$?
  cat output/fetch-releases.log output/fetch-blogs.log output/fetch-events.log
  # events is best-effort (deferred to the deck-data agent); releases/blogs are required.
  [[ "$rc_evt"  -ne 0 ]] && warn "events prep returned $rc_evt (deferred to agent — non-fatal)"
  if [[ "$rc_rel" -ne 0 || "$rc_blog" -ne 0 ]]; then
    warn "required fetcher failed (releases=$rc_rel blogs=$rc_blog); see output/fetch-*.log"
    exit 1
  fi
else
  warn "SKIP_FETCH=1 — using existing data/*.json"
fi

# --- Phase 2: working copy -------------------------------------------------------------
log "Phase 2: working copy"
COPY_ARGS=()
[[ -n "${SOURCE_PPTX:-}" ]] && COPY_ARGS=("$SOURCE_PPTX")
WORKING_COPY="$("$PY" scripts/make_working_copy.py "${COPY_ARGS[@]}")"
echo "working copy: $WORKING_COPY"

# --- Phase 2b: slide-structure map ------------------------------------------------------
# Deterministically extract each automatable slide's editable shapes (geometry + roles +
# hyperlinks) so the build agent never re-discovers layout (the biggest time/quota sink).
# Non-fatal: the build agent can fall back to its own geometry dump if this is missing.
log "Phase 2b: slide-structure map"
if "$PY" scripts/map_slides.py "$WORKING_COPY" > output/map-slides.log 2>&1; then
  echo "structure map: data/slide-structure.json"
else
  warn "map_slides.py failed (build will self-discover layout); see output/map-slides.log"
fi

# --- Phase 3: data enrichment (agent) --------------------------------------------------
if [[ "${SKIP_DATA:-0}" != "1" ]]; then
  run_copilot deck-data "$(cat <<EOF
Run the deck-data enrichment phase for the auto-pptx pipeline in $REPO_ROOT.
Follow your agent instructions exactly. Read config/sources.yaml and data/events.request.json.
Tasks:
  1) Fill data/releases.json highlights: prioritize products.<product>.latest.highlight (the build's
     new col3), keep recent[0] consistent, via web_fetching each changelog_url (fetch each URL once).
  2) Populate data/releases.json -> terraform_enterprise.recent + .latest from its releases_page.
  3) Produce data/events.json per data/events.request.json (upcoming events only, never fabricate).
Only write JSON files under data/. Do not touch any .pptx.
EOF
)"
else
  warn "SKIP_DATA=1 — skipping agent enrichment"
fi

# --- Phase 3b: deterministic release-slide edits ---------------------------------------
# Apply the rolling shift/refresh standard to release slides (7,8,11,12) in PYTHON, not the AI
# agent — far faster/cheaper and exact. Also reconciles mislabeled versions by their release DATE
# (the authoritative key), e.g. fixing a "1.15.4" entry dated 2026-05-27 that is really 1.15.5.
# Writes data/release-edit-report.json; slides too ambiguous to edit safely are marked
# fallback_slides[] for the build agent. Non-fatal: any failure just defers all release slides.
if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  log "Phase 3b: deterministic release-slide edits"
  ORIG_DECK="${SOURCE_PPTX:-$(ls -1 "$REPO_ROOT"/*.pptx 2>/dev/null | grep -v '/~\$' | head -1)}"
  if "$PY" scripts/edit_release_slides.py "$WORKING_COPY" --original "$ORIG_DECK" \
        > output/edit-release.log 2>&1; then
    cat output/edit-release.log
    FB="$("$PY" -c 'import json;print(",".join(map(str,json.load(open("data/release-edit-report.json")).get("fallback_slides",[]))) or "none")' 2>/dev/null || echo "?")"
    echo "release editor: handled deterministically; fallback slides for agent: $FB"
  else
    warn "edit_release_slides.py failed — build agent will handle ALL release slides; see output/edit-release.log"
  fi
fi

# --- Phase 4: build (agent) ------------------------------------------------------------
if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  run_copilot pptx-orchestrator "$(cat <<EOF
Run the build phase for the auto-pptx pipeline in $REPO_ROOT.
The working copy to edit IN PLACE is:
  $WORKING_COPY
Follow your agent instructions and the hashicorp-deck-style + pptx skills exactly.
Read corrections/corrections.md first and apply every applicable rule.
Use data/slide-structure.json (shape roles/geometry) so you do NOT re-discover layout.
Update only the slides in config/slide-map.yaml using data/releases.json, data/blogs.json,
data/events.json.
RELEASE SLIDES (7,8,11,12) ARE MOSTLY DONE FOR YOU: a deterministic Python pass already applied
the rolling shift/refresh standard and wrote data/release-edit-report.json. Read it first. ONLY
edit the release slides listed in its "fallback_slides" array (those were too ambiguous for the
safe automation, e.g. a new-month shift across multi-entry columns); for those, apply the standard
by hand. Do NOT touch the release slides in "handled_slides" — they are already correct (just leave
them). Still update the col3 "highlight" bullets from data if the editor only changed the version.
For any fallback release slide apply the ROLLING shift/refresh standard:
compare products.<product>.latest.month_label to the current col3 header — same month refreshes
col3 only, a newer month shifts the (header,entry) pairs left; never blank col1/col2.
Preserve all formatting (edit existing runs only). Hide slides with no fresh data (never delete).
Run the style_check guard, then clean + pack back to the same working copy path.
EOF
)"
else
  warn "SKIP_BUILD=1 — skipping build"
fi

# --- Phase 4b: deterministic verify (cheap, exact gate before the AI reviewer) ---------
# Machine-checks integrity (slide count), hide state (the RIGHT way: <p:sld show="0">), style,
# and that each release slide's latest column carries products.<p>.latest. Writes a report the
# AI reviewer reads, so it never has to eyeball XML. Advisory: a FAIL is surfaced but non-fatal.
if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  log "Phase 4b: deterministic verify"
  ORIG_DECK="${SOURCE_PPTX:-$(ls -1 "$REPO_ROOT"/*.pptx 2>/dev/null | grep -v '/~\$' | head -1)}"
  VERIFY_LOG="output/verify-$(date +%Y%m%d-%H%M).txt"
  if "$PY" scripts/verify_deck.py "$WORKING_COPY" --original "$ORIG_DECK" | tee "$VERIFY_LOG"; then
    echo "verify: PASS ($VERIFY_LOG)"
  else
    warn "verify_deck reported a FAIL — see $VERIFY_LOG (reviewer will assess)"
  fi
fi

# --- Phase 5: review (agent) -----------------------------------------------------------
if [[ "${SKIP_REVIEW:-0}" != "1" ]]; then
  run_copilot deck-reviewer "$(cat <<EOF
Review the updated deck produced by the build phase:
  $WORKING_COPY
Follow your agent instructions. First READ the deterministic report at the latest output/verify-*.txt
(integrity, hide audit done the correct way, style, release-column latest checks) and treat its
machine-checked findings as ground truth — do not re-derive them by hand. Then add the judgment-based
checks: data accuracy vs data/*.json, faithful highlight summaries, correct hyperlink targets, and
scope. Verify hiding via <p:sld show="0"> on each slide ROOT (NOT presentation.xml).
Write output/review-<timestamp>.md and record any durable learnings via scripts/capture_correction.py.
Do NOT modify the deck (report only) unless told to fix.
EOF
)"
else
  warn "SKIP_REVIEW=1 — skipping review"
fi

log "Done. Output deck: $WORKING_COPY"
echo "Review + build notes (if any) are in output/."
