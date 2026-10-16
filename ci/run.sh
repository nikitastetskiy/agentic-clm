#!/usr/bin/env bash
#
# Pipeline entry point. The pipeline property `pipeline-mode` (set per trigger) picks the job:
#   weekly     build this week's deck and publish it           (timed trigger; the default)
#   self-heal  repair the repository after a failed build      (webhook trigger, or by hand)
#   preflight  only report what this runner can reach and which settings are present
#
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
# shellcheck source=ci/lib.sh
source ci/lib.sh
MODE="$(prop pipeline-mode weekly)"
case "$MODE" in
  weekly)    exec bash ci/weekly-build.sh ;;
  self-heal) exec bash ci/self-heal.sh ;;
  preflight) ci_setup; ci_preflight ;;
  *) echo "unknown pipeline-mode '$MODE' (expected weekly, self-heal or preflight)" >&2; exit 2 ;;
esac
