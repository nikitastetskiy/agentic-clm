#!/usr/bin/env bash
#
# Shared helpers for the pipeline entry points (ci/weekly-build.sh, ci/self-heal.sh). Source it.
#
# Everything here is runner-agnostic. On IBM Secure Pipelines Service / IBM Cloud DevSecOps the
# values come from the pipeline's environment properties (read with pipelinectl's get_env /
# get_secret, which the platform injects into the stage); anywhere else — a laptop, the Docker image,
# another CI — the same names are read from environment variables. Property `ica-api-key` is the
# variable ICA_API_KEY, and so on.
#
# Secrets are never echoed, never written to disk and never placed on a command line.
#
# CI_DRY_RUN=1 rehearses a pipeline on any machine: everything runs for real (fetch, build, verify,
# even the repair agent) except what leaves the machine — no push, no release, no pull request, no
# issue, no webhook; each of those is printed instead.

ci_log()  { printf '\n\033[1;36m[ci] %s\033[0m\n' "$*"; }
ci_warn() { printf '\033[1;33m[ci:warn] %s\033[0m\n' "$*"; }

_ci_var() { printf '%s' "$1" | tr '[:lower:]-' '[:upper:]_'; }

# prop <name> [default] — a plain pipeline property
prop() {
  local name="$1" default="${2:-}" var value
  var="$(_ci_var "$name")"
  value="${!var:-}"
  if [[ -z "$value" ]] && command -v get_env >/dev/null 2>&1; then
    value="$(get_env "$name" "" 2>/dev/null || true)"
  fi
  if [[ -z "$value" && -r "/config/environment-properties/$name" ]]; then
    value="$(cat "/config/environment-properties/$name")"
  fi
  printf '%s' "${value:-$default}"
}

# secret <name> — a secure pipeline property (assign it; do not echo it)
secret() {
  local name="$1" var value
  var="$(_ci_var "$name")"
  value="${!var:-}"
  if [[ -z "$value" ]] && command -v get_secret >/dev/null 2>&1; then
    value="$(get_secret "$name" "" 2>/dev/null || true)"
  fi
  if [[ -z "$value" && -r "/config/secure-properties/$name" ]]; then
    value="$(cat "/config/secure-properties/$name")"
  fi
  printf '%s' "$value"
}

# ci_setup — resolve properties and secrets into the environment the scripts expect.
ci_setup() {
  set +x   # never trace: the lines below handle secrets
  REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  cd "$REPO_ROOT" || exit 2
  if [[ -d "$REPO_ROOT/.venv/bin" ]]; then export PATH="$REPO_ROOT/.venv/bin:$PATH"; fi
  PY="${PYTHON:-python3}"

  export ENRICH_MODEL; ENRICH_MODEL="$(prop enrich-model ica-gateway/claude-haiku-4-5)"
  export FIX_MODEL;    FIX_MODEL="$(prop fix-model ica-gateway/claude-opus-5-5)"
  export EVENT_SCOPE;  EVENT_SCOPE="$(prop event-scope "")"
  export STRICT;       STRICT="$(prop strict 0)"
  BASE_BRANCH="$(prop base-branch main)"
  RELEASE_KEEP="$(prop release-keep 0)"
  DRY=()
  if [[ "${CI_DRY_RUN:-0}" == "1" ]]; then
    DRY=(--dry-run)
    ci_warn "CI_DRY_RUN=1 — nothing will be pushed, published or reported"
  fi

  # LLM gateway. The provider block is handed to opencode inline, so neither the gateway address nor
  # the key has to be committed; the key itself stays in the environment ({env:…} is resolved by
  # opencode). On a laptop none of this is set and opencode uses its own stored login.
  export ICA_API_KEY;  ICA_API_KEY="$(secret ica-api-key)"
  export ICA_BASE_URL; ICA_BASE_URL="$(prop ica-base-url "")"
  if [[ -n "$ICA_API_KEY" && -n "$ICA_BASE_URL" ]]; then
    export OPENCODE_CONFIG_CONTENT
    OPENCODE_CONFIG_CONTENT="$("$PY" - <<'PYEOF'
import json, os
providers = {}
for model in (os.environ["ENRICH_MODEL"], os.environ["FIX_MODEL"]):   # "<provider>/<model id>"
    provider, _, name = model.partition("/")
    if name:
        providers.setdefault(provider, {
            "npm": "@ai-sdk/openai-compatible", "name": "LLM gateway",
            "options": {"baseURL": os.environ["ICA_BASE_URL"], "apiKey": "{env:ICA_API_KEY}",
                        "headerTimeout": 300000, "chunkTimeout": 120000},
            "models": {}})["models"][name] = {"name": name}
print(json.dumps({"share": "disabled", "autoupdate": False, "provider": providers}))
PYEOF
)"
  fi

  # GitHub: where the artifact is published and where fixes are proposed.
  export GIT_TOKEN; GIT_TOKEN="$(secret git-token)"
  if [[ "${CI_DRY_RUN:-0}" == "1" && -z "$GIT_TOKEN" ]]; then GIT_TOKEN="dry-run"; fi
  export GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-auto-pptx pipeline}"
  export GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-$(prop bot-email auto-pptx-pipeline@noreply.invalid)}"
  export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME" GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"

  SELFHEAL_WEBHOOK_URL="$(secret selfheal-webhook-url)"
  SELFHEAL_WEBHOOK_TOKEN="$(secret selfheal-webhook-token)"
}

# ci_preflight — say plainly what this runner can and cannot reach. Never fails the run: a blocked
# source shows up again, with consequences, in the step that needs it.
ci_preflight() {
  ci_log "preflight"
  local tool
  for tool in "$PY" git bash opencode soffice; do
    if command -v "$tool" >/dev/null 2>&1; then printf '  %-10s ok\n' "$tool"; else printf '  %-10s MISSING\n' "$tool"; fi
  done
  "$PY" - <<'PYEOF'
import os, socket, sys, urllib.parse
sys.path.insert(0, "scripts")
from lib.github import remote_coordinates
hosts = ["api.releases.hashicorp.com", "developer.hashicorp.com", "www.hashicorp.com",
         "raw.githubusercontent.com"]
try:
    hosts.append(remote_coordinates()[0])
except Exception:
    pass
if os.environ.get("ICA_BASE_URL"):
    hosts.append(urllib.parse.urlparse(os.environ["ICA_BASE_URL"]).hostname)
for host in hosts:
    try:
        socket.create_connection((host, 443), timeout=8).close()
        print(f"  {host:<32} reachable")
    except OSError as exc:
        print(f"  {host:<32} UNREACHABLE ({exc})")
for name in ("ICA_API_KEY", "ICA_BASE_URL", "GIT_TOKEN"):
    print(f"  {name:<32} {'set' if os.environ.get(name) else 'NOT SET'}")
try:
    from playwright.sync_api import sync_playwright  # noqa: F401
    print(f"  {'playwright':<32} installed")
except ImportError:
    print(f"  {'playwright':<32} NOT INSTALLED (events and blog listings unavailable)")
PYEOF
}

# ci_git_push <refspec> — push over https with the pipeline token. git asks for the password through
# ci/askpass.sh, which answers from the environment, so the token is never in a URL, in git's config
# or on a command line.
ci_git_push() {
  local refspec="$1" host_repo
  if [[ "${CI_DRY_RUN:-0}" == "1" ]]; then echo "  [dry-run] git push origin ${refspec}"; return 0; fi
  if [[ -z "$GIT_TOKEN" ]]; then ci_warn "no git-token; cannot push"; return 1; fi
  host_repo="$("$PY" -c 'import sys; sys.path.insert(0, "scripts"); from lib.github import remote_coordinates as r; h, p = r(); print(f"{h}/{p}")')" || return 1
  GIT_ASKPASS="$REPO_ROOT/ci/askpass.sh" GIT_TERMINAL_PROMPT=0 \
    git -c credential.helper= push "https://x-access-token@${host_repo}.git" "$refspec"
}

# ci_trigger_selfheal <failed step> <exit code> — start the self-heal pipeline through its generic
# webhook trigger. Returns non-zero when no webhook is configured or the call fails.
ci_trigger_selfheal() {
  if [[ "${CI_DRY_RUN:-0}" == "1" ]]; then echo "  [dry-run] would trigger the self-heal pipeline ($1, exit $2)"; return 0; fi
  if [[ -z "$SELFHEAL_WEBHOOK_URL" ]]; then
    ci_warn "no selfheal-webhook-url configured; start the self-heal pipeline by hand"
    return 1
  fi
  SELFHEAL_WEBHOOK_URL="$SELFHEAL_WEBHOOK_URL" SELFHEAL_WEBHOOK_TOKEN="$SELFHEAL_WEBHOOK_TOKEN" \
  FAILED_STEP="$1" FAILED_CODE="$2" "$PY" - <<'PYEOF'
import json, os, sys, urllib.request
body = {"description": f"auto-pptx weekly build failed at {os.environ['FAILED_STEP']} (exit {os.environ['FAILED_CODE']})",
        "properties": [{"name": "pipeline-mode", "type": "TEXT", "value": "self-heal"},
                       {"name": "failed-run", "type": "TEXT", "value": os.environ.get("PIPELINE_RUN_URL", "")}]}
req = urllib.request.Request(os.environ["SELFHEAL_WEBHOOK_URL"], data=json.dumps(body).encode(), method="POST",
                             headers={"Content-Type": "application/json", "token": os.environ["SELFHEAL_WEBHOOK_TOKEN"]})
try:
    with urllib.request.urlopen(req, timeout=60) as resp:
        print(f"  self-heal pipeline triggered (HTTP {resp.status})")
except Exception as exc:  # noqa: BLE001
    print(f"  could not trigger the self-heal pipeline: {exc}")
    sys.exit(1)
PYEOF
}

# ci_commit_state <message> — keep the small git-tracked build state (blog history, stored wording)
# in step with what this build learned, so next week starts from it. Best-effort.
ci_commit_state() {
  if [[ "${CI_DRY_RUN:-0}" == "1" ]]; then
    # A rehearsal must leave the checkout's history alone: no staging, no commit.
    if [[ -n "$(git status --porcelain -- state/)" ]]; then
      echo "  [dry-run] would commit state/ (\"$1\") and push it to ${BASE_BRANCH}"
    else
      echo "  state/ unchanged"
    fi
    return 0
  fi
  git add state/ 2>/dev/null || return 0
  if git diff --cached --quiet -- state/; then
    echo "  state/ unchanged"
    return 0
  fi
  git commit -q -m "$1" -- state/ || return 0
  if ci_git_push "HEAD:refs/heads/${BASE_BRANCH}"; then
    echo "  state/ pushed to ${BASE_BRANCH}"
  else
    ci_warn "could not push state/ to ${BASE_BRANCH} (protected branch or read-only token); the next build starts from the old state"
  fi
}

# ci_publish [note] — publish the verified deck as a GitHub release, with a PDF preview when
# LibreOffice is in the image.
ci_publish() {
  local note="${1:-}" deck preview=() keep=()
  deck="$("$PY" -c 'import sys; sys.path.insert(0, "scripts"); from lib.common import read_json; print(read_json("build-report.json").get("deck", ""))')"
  if command -v soffice >/dev/null 2>&1 && [[ -f "$deck" ]]; then
    if soffice --headless --convert-to pdf --outdir output "$deck" >/dev/null 2>&1; then
      preview=(--extra "output/$(basename "${deck%.pptx}").pdf")
    else
      ci_warn "PDF preview could not be rendered (deck is still published)"
    fi
  fi
  [[ "${RELEASE_KEEP:-0}" -gt 0 ]] && keep=(--keep "$RELEASE_KEEP")
  "$PY" scripts/publish_release.py ${note:+--note "$note"} ${preview[@]+"${preview[@]}"} \
      ${keep[@]+"${keep[@]}"} ${DRY[@]+"${DRY[@]}"}
}
