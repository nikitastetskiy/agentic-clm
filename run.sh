#!/usr/bin/env bash
#
# auto-pptx pipeline orchestrator.
#
# Phases:
#   0. skill-updater  — ensure the vendored pptx skill matches its pin (advisory)
#   1. fetch          — deterministic Python: releases + changelog highlights + TFE + blogs + events
#   1b. event images   — deterministic Python (Playwright): event leadspace screenshots (best-effort)
#   2. copy           — timestamped working copy (original is never touched)
#   3. data (agent)   — deck-data: events prose + light highlight polish (no web_fetch of changelogs/TFE)
#   3b-d. det. edits   — deterministic Python: release (7,8,11,12) + blog (9,13) + event (10,14,18-22) slides
#   3e. gate          — deterministic verify_deck; if it PASSES, the build+review AGENTS are skipped
#   4. build (agent)  — pptx-orchestrator: ONLY when the gate failed (or FORCE_AGENTS=1) — fix the gaps
#   4b. re-verify      — deterministic verify_deck again, after the build agent edited the deck
#   5. review (agent) — deck-reviewer: ONLY when the build agent ran (or FORCE_AGENTS=1)
#
# Env (see .env.example):
#   COPILOT_MODEL          model override (optional)
#   SKILL_UPDATE_POLICY    check | update   (default: check)
#   SKIP_FETCH/SKIP_DATA/SKIP_BUILD/SKIP_REVIEW = 1 to skip a phase
#   FORCE_AGENTS           1 to always run the build+review agents even when the deterministic gate passes
#   SKIP_EVENT_IMAGES      1 to skip the Playwright event-leadspace capture (Phase 1b)
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
  log "Phase 1: fetch (releases+changelogs+TFE, blogs, events-prep) [parallel]"
  mkdir -p output
  # The releases chain runs in series (all three write data/releases.json): the API fetch first,
  # then the deterministic changelog-highlight extractor and the TFE releases-page parser. This
  # moves the slow web_fetch/summarize work out of the deck-data agent (deck-data ~8min -> ~2-3min).
  ( "$PY" scripts/fetch_releases.py \
      && "$PY" scripts/fetch_changelogs.py \
      && "$PY" scripts/fetch_tfe_releases.py ) > output/fetch-releases.log 2>&1 & pid_rel=$!
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

# --- Phase 1b: event photo visuals (Playwright; best-effort) ----------------------------
# Captures event visuals for the featured slides. Per-event captures are quality-screened so
# registration-page fragments (buttons/forms/large blank white areas) do not become deck photos; each
# slide prefers its OWN per-city leadspace hero, with a clean HashiCorp series hero only as a fallback
# for events whose page has no leadspace.
if [[ "${SKIP_FETCH:-0}" != "1" && "${SKIP_EVENT_IMAGES:-0}" != "1" ]]; then
  log "Phase 1b: event photo visuals (Playwright, best-effort)"
  if "$PY" scripts/fetch_event_images.py > output/fetch-event-images.log 2>&1; then
    tail -n 3 output/fetch-event-images.log
  else
    warn "event-image capture returned non-zero (non-fatal — existing photos kept); see output/fetch-event-images.log"
  fi
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
  1) Release highlights in data/releases.json are now PRE-FILLED deterministically by
     scripts/fetch_changelogs.py (each entry's "highlight" + the latest's "changelog_excerpt").
     Do NOT web_fetch changelogs. Only IMPROVE wording where a "highlight" reads awkwardly or is
     missing, using the local "changelog_excerpt" — keep it one factual present-tense sentence and
     keep products.<product>.latest and recent[0] consistent. Never invent; leave "" if no excerpt.
  2) terraform_enterprise is now PRE-FILLED deterministically by scripts/fetch_tfe_releases.py.
     If its "status" is "ok", optionally polish each release "highlight" from its release_notes_url;
     if "status" is "stale_source" or "unavailable", leave it as-is (do NOT refetch — the guard has
     already decided slide 8 stays unchanged).
  3) Produce data/events.json per data/events.request.json (upcoming events only, never fabricate).
     NOTE: events are now fetched deterministically by scripts/fetch_events.py (Playwright). If
     data/events.json status=="ok", DO NOT change its facts (dates/titles/urls/buckets) — only fill
     the empty prose: each webinar "description" (one short sentence), and for each featured event the
     "location" plus "description_points" (a list of 2-3 rich full-sentence bullets, ~15-30 words each,
     matching the inviting style of the original event slides; ~300-500 chars total) the slide renders
     as its description. Only fall back to web_fetch + the request file if status != "ok".
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

# --- Phase 3c: deterministic blog-slide edits ------------------------------------------
# Refresh the top-N recent blog entries on slides 9 (Terraform) & 13 (Vault) from data/blogs.json
# in PYTHON: rewrite the date + title runs and repoint each "See here" hyperlink via a FRESH rel
# (shared rIds are never mutated). Older entries stay as history. Writes data/blog-edit-report.json.
# Non-fatal: a failure just leaves the blog slides for the build agent.
if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  log "Phase 3c: deterministic blog-slide edits"
  if "$PY" scripts/edit_blog_slides.py "$WORKING_COPY" --original "$ORIG_DECK" \
        > output/edit-blogs.log 2>&1; then
    cat output/edit-blogs.log
  else
    warn "edit_blog_slides.py failed — build agent will handle blog slides; see output/edit-blogs.log"
  fi
fi

# --- Phase 3d: deterministic event-slide edits -----------------------------------------
# Fill the webinar slots (10,14) and featured/in-person slides (18-22) from data/events.json in
# PYTHON: dates/titles/See-here+Register hyperlinks (fresh rels), descriptions collapsed to the one
# events.json sentence, featured-event photos set to a consistent series visual where applicable, and
# slides with no event HIDDEN (show="0", never deleted). Runs AFTER deck-data filled the prose. Writes
# data/event-edit-report.json. Non-fatal.
if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  log "Phase 3d: deterministic event-slide edits"
  if "$PY" scripts/edit_event_slides.py "$WORKING_COPY" --original "$ORIG_DECK" \
        > output/edit-events.log 2>&1; then
    cat output/edit-events.log
  else
    warn "edit_event_slides.py failed — build agent will handle event slides; see output/edit-events.log"
  fi
fi

# --- Phase 3e: deterministic gate (decides whether the AI build/review are even needed) -
# If the deterministic editors already produced a deck that PASSES verify_deck, the build agent
# (which only fixes deterministic gaps, including photo-pattern failures) and the AI reviewer add no
# value — skip them to save
# ~30-40 min + credits. A FAIL falls back to the agents. Set FORCE_AGENTS=1 to always run them.
GATE="unknown"
if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  log "Phase 3e: deterministic gate"
  ORIG_DECK="${SOURCE_PPTX:-$(ls -1 "$REPO_ROOT"/*.pptx 2>/dev/null | grep -v '/~\$' | head -1)}"
  VERIFY_LOG="output/verify-$(date +%Y%m%d-%H%M).txt"
  if "$PY" scripts/verify_deck.py "$WORKING_COPY" --original "$ORIG_DECK" | tee "$VERIFY_LOG"; then
    GATE="pass"; echo "deterministic gate: PASS ($VERIFY_LOG)"
  else
    GATE="fail"; warn "deterministic gate: FAIL — the build agent will fix the gaps (see $VERIFY_LOG)"
  fi
fi

# --- Phase 4: build (agent) — only when the gate did NOT already pass -------------------
BUILD_RAN=0
if [[ "${SKIP_BUILD:-0}" == "1" ]]; then
  warn "SKIP_BUILD=1 — skipping build"
elif [[ "$GATE" == "pass" && "${FORCE_AGENTS:-0}" != "1" ]]; then
  log "Phase 4: build agent SKIPPED — deterministic gate already PASSED (set FORCE_AGENTS=1 to force)"
else
  BUILD_RAN=1
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
BLOG SLIDES (9,13) AND EVENT SLIDES (10,14,18-22) ARE ALSO ALREADY DONE deterministically by
scripts/edit_blog_slides.py and scripts/edit_event_slides.py (see data/blog-edit-report.json and
data/event-edit-report.json). DO NOT touch slides 9, 13, 10, 14, 18, 19, 20, 21, 22 — their text,
hyperlinks, and hide state are correct; re-editing them risks regressions. If a report shows a slide
was skipped/failed (its entry is missing or "changed": false where data existed), or verify_deck flags
event/photo consistency, only then fix that specific slide by hand following config/slide-map.yaml.
For any fallback release slide apply the ROLLING shift/refresh standard:
compare products.<product>.latest.month_label to the current col3 header — same month refreshes
col3 only, a newer month shifts the (header,entry) pairs left; never blank col1/col2.
Preserve all formatting (edit existing runs only). Hide slides with no fresh data (never delete).
Run the style_check guard, then clean + pack back to the same working copy path.
EOF
)"
fi

# --- Phase 4b: deterministic re-verify — only after the build agent edited the deck ----
# (The Phase 3e gate already verified the deterministic-only deck; re-check only when the agent
# touched it.) Machine-checks integrity, hide state (<p:sld show="0">), style, columns, coverage.
if [[ "$BUILD_RAN" == "1" ]]; then
  log "Phase 4b: deterministic re-verify (after build agent)"
  ORIG_DECK="${SOURCE_PPTX:-$(ls -1 "$REPO_ROOT"/*.pptx 2>/dev/null | grep -v '/~\$' | head -1)}"
  VERIFY_LOG="output/verify-$(date +%Y%m%d-%H%M).txt"
  if "$PY" scripts/verify_deck.py "$WORKING_COPY" --original "$ORIG_DECK" | tee "$VERIFY_LOG"; then
    GATE="pass"; echo "verify: PASS ($VERIFY_LOG)"
  else
    GATE="fail"; warn "verify_deck reported a FAIL — see $VERIFY_LOG (reviewer will assess)"
  fi
fi

# --- Phase 5: review (agent) — only when the build agent ran (or forced) ----------------
if [[ "${SKIP_REVIEW:-0}" == "1" ]]; then
  warn "SKIP_REVIEW=1 — skipping review"
elif [[ "$BUILD_RAN" != "1" && "$GATE" == "pass" && "${FORCE_AGENTS:-0}" != "1" ]]; then
  log "Phase 5: review agent SKIPPED — deterministic gate passed, no agent build needed (set FORCE_AGENTS=1 to force)"
else
  run_copilot deck-reviewer "$(cat <<EOF
Review the updated deck produced by the build phase:
  $WORKING_COPY
Follow your agent instructions. First READ the deterministic report at the latest output/verify-*.txt
(integrity, hide audit done the correct way, style, release-column latest checks) and treat its
machine-checked findings as ground truth — do not re-derive them by hand. Then add the judgment-based
checks: data accuracy vs data/*.json, faithful highlight summaries, correct hyperlink targets, featured
event photo consistency across slides 18-22 (no registration-page fragments/buttons/forms/blank-white
captures), and scope. Verify hiding via <p:sld show="0"> on each slide ROOT (NOT presentation.xml).
Write output/review-<timestamp>.md and record any durable learnings via scripts/capture_correction.py.
Do NOT modify the deck (report only) unless told to fix.
EOF
)"
fi

log "Done. Output deck: $WORKING_COPY"

# --- Final: human-readable run summary -------------------------------------------------
# Print a table of what each automatable slide group changed, the verify gate result, and an
# explicit "please review" notice with the path to the finished deck. Never fails the run.
"$PY" scripts/summarize_run.py "$WORKING_COPY" || \
  echo "Output deck: $WORKING_COPY  (review + build notes are in output/)"
