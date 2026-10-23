#!/usr/bin/env bash
#
# auto-pptx weekly build — builds a fresh deck of the automated slides from public sources.
#
#   1. fetch    deterministic Python: releases (+changelogs, TFE, Vault release notes), HCP
#               changelogs, blogs, events                                       -> data/*.json
#               (+ an advisory check that the vendored pptx skill matches its upstream)
#   2. plan     data + stored wording -> what every slide will say              -> data/deck-plan.json
#   3. enrich   OPTIONAL: an LLM agent words items nobody has worded yet, once  -> state/enrichment.json
#               (then the plan is recomputed). Never fails a build.
#   4. build    template + plan -> the deck                                     -> output/<name>-<date>.pptx
#   5. verify   read the deck back and check it against plan, data and template -> data/verify-report.json
#   6. summary  what is on each slide, what was left out, the gate's verdict    -> output/summary-<date>.md
#
# Steps 1, 2, 4, 5 are plain Python with no model involved: the same data gives the same deck.
#
# Env (see .env.example):
#   SKIP_FETCH=1     reuse data/*.json from the last fetch
#   ENRICH=auto|1|0  enrichment step: auto (default) = run it when the gateway key and address are set
#                    (ICA_API_KEY, ICA_BASE_URL: a direct call) or opencode is installed (its own login)
#   ENRICH_MODEL     model for the enrichment agent        (default ica-gateway/claude-haiku-5-5)
#   EVENT_SCOPE      region scope for events, e.g. "EMEA,MENA" (default), "GLOBAL"
#   BUILD_DATE       pin "today" (YYYY-MM-DD) to reproduce a build from saved data
#   STRICT=1         also fail when an optional source (the events page) was unavailable
#
# Exit codes: 0 deck built and verified · 10 a required fetch failed · 20 plan failed ·
#             30 build failed · 40 the gate rejected the deck · 50 degraded (only with STRICT=1)
# On failure output/failure.json names the step and carries the tail of its log.
#
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# Prefer the project venv for this run and for anything it launches.
if [[ -d "$REPO_ROOT/.venv/bin" ]]; then
  export PATH="$REPO_ROOT/.venv/bin:$PATH"
fi
if [[ -f .env ]]; then
  set -a; # shellcheck disable=SC1091
  source .env; set +a
fi

PY="${PYTHON:-python3}"
export EVENT_SCOPE="${EVENT_SCOPE:-}"
mkdir -p output data
STAMP="$(date +%Y%m%d-%H%M%S)"
LOG="output/build-${STAMP}.log"
rm -f output/failure.json

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*" | tee -a "$LOG"; }
warn() { printf '\033[1;33m[warn] %s\033[0m\n' "$*" | tee -a "$LOG"; }

# die <step> <exit code> <log of the failed step>
die() {
  "$PY" scripts/summarize_build.py --failure "$1" "$2" "$3" || true
  printf '\n\033[1;31m[failed] step "%s" (exit %s) — see %s and output/failure.json\033[0m\n' "$1" "$2" "$3" | tee -a "$LOG"
  exit "$2"
}

# step <name> <exit code on failure> <command...> : run, mirror to the build log, stop on failure
step() {
  local name="$1" code="$2"; shift 2
  local step_log="output/step-${name}.log"
  if ! "$@" > "$step_log" 2>&1; then
    cat "$step_log" | tee -a "$LOG"
    die "$name" "$code" "$step_log"
  fi
  cat "$step_log" | tee -a "$LOG"
}

# --- 1. fetch -----------------------------------------------------------------------------
if [[ "${SKIP_FETCH:-0}" != "1" ]]; then
  log "1/6 fetch (releases, changelogs, blogs, events) [parallel]"
  # The releases chain runs in series: each step adds to data/releases.json.
  ( "$PY" scripts/fetch_releases.py && "$PY" scripts/fetch_changelogs.py \
      && "$PY" scripts/fetch_tfe_releases.py && "$PY" scripts/fetch_release_notes.py ) \
      > output/fetch-releases.log 2>&1 & pid_rel=$!
  "$PY" scripts/fetch_hcp_changelogs.py > output/fetch-changelogs.log 2>&1 & pid_log=$!
  "$PY" scripts/fetch_blogs.py          > output/fetch-blogs.log      2>&1 & pid_blog=$!
  "$PY" scripts/fetch_events.py         > output/fetch-events.log     2>&1 & pid_evt=$!
  # Advisory only: is the vendored pptx skill (whose unpack / pack / clean / add_slide scripts the
  # build and the gate run) behind its upstream? .github/skills/skill-updater; never fails the build.
  "$PY" .github/skills/skill-updater/scripts/check_updates.py --check > output/skills-check.log 2>&1 & pid_skill=$!
  rc_rel=0; rc_log=0; rc_blog=0; rc_evt=0; rc_skill=0
  wait "$pid_rel"  || rc_rel=$?
  wait "$pid_log"  || rc_log=$?
  wait "$pid_blog" || rc_blog=$?
  wait "$pid_evt"  || rc_evt=$?
  wait "$pid_skill" || rc_skill=$?
  cat output/fetch-releases.log output/fetch-changelogs.log output/fetch-blogs.log output/fetch-events.log \
      output/skills-check.log | tee -a "$LOG"
  [[ "$rc_skill" -ne 0 ]] && warn "skill check exited ${rc_skill} (10 = update available); building with the pinned pptx skill"
  [[ "$rc_rel"  -ne 0 ]] && die fetch-releases   10 output/fetch-releases.log
  [[ "$rc_log"  -ne 0 ]] && die fetch-changelogs 10 output/fetch-changelogs.log
  [[ "$rc_blog" -ne 0 ]] && die fetch-blogs      10 output/fetch-blogs.log
  # The events fetcher exits 0 when the page is merely unreachable (the deck is then built without
  # event slides and flagged); a non-zero exit means the page rendered but could not be parsed.
  [[ "$rc_evt"  -ne 0 ]] && die fetch-events     10 output/fetch-events.log
else
  warn "SKIP_FETCH=1 — using existing data/*.json"
fi

# --- 2. plan ------------------------------------------------------------------------------
log "2/6 plan"
step plan 20 "$PY" scripts/plan_deck.py

# --- 3. enrich (optional, never fatal) ----------------------------------------------------
ENRICH="${ENRICH:-auto}"
if [[ "$ENRICH" == "0" ]]; then
  log "3/6 enrich — skipped (ENRICH=0)"
elif [[ "$ENRICH" == "auto" && ( -z "${ICA_API_KEY:-}" || -z "${ICA_BASE_URL:-}" ) ]] && ! command -v opencode >/dev/null 2>&1; then
  log "3/6 enrich — skipped (no gateway key and no opencode; rule-based wording)"
else
  log "3/6 enrich (${ENRICH_MODEL:-ica-gateway/claude-haiku-5-5})"
  if "$PY" scripts/enrich.py > output/step-enrich.log 2>&1; then
    cat output/step-enrich.log | tee -a "$LOG"
    step plan 20 "$PY" scripts/plan_deck.py    # pick up the wording that was just stored
  else
    cat output/step-enrich.log | tee -a "$LOG"
    warn "enrichment step failed — continuing with rule-based wording"
  fi
fi

# --- 4. build -----------------------------------------------------------------------------
log "4/6 build"
step build 30 "$PY" scripts/build_deck.py

# --- 5. verify ----------------------------------------------------------------------------
log "5/6 verify"
step verify 40 "$PY" scripts/verify_build.py

# --- 6. summary ---------------------------------------------------------------------------
log "6/6 summary"
"$PY" scripts/summarize_build.py | tee -a "$LOG"

STATUS="$("$PY" -c 'import sys; sys.path.insert(0, "scripts"); from lib.common import read_json; print(read_json("deck-plan.json")["build"]["status"])' 2>/dev/null || echo unknown)"
if [[ "$STATUS" != "ok" ]]; then
  warn "build is DEGRADED: an optional source was unavailable and its slides were left out"
  if [[ "${STRICT:-0}" == "1" ]]; then
    die degraded 50 "$LOG"
  fi
fi
exit 0
