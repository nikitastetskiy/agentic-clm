# auto-pptx — convenience targets
.DEFAULT_GOAL := help
PY ?= python3

.PHONY: help build fetch plan deck verify summary test template enrich preflight publish-check \
        build-image docker-run clean run-local skills-check skills-update

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'

# --- weekly build -------------------------------------------------------------------------
build: ## Build this week's deck: fetch -> plan -> enrich -> build -> verify (output/*.pptx)
	bash build.sh

fetch: ## Refresh data/*.json from the public sources only
	$(PY) scripts/fetch_releases.py
	$(PY) scripts/fetch_changelogs.py
	$(PY) scripts/fetch_tfe_releases.py
	$(PY) scripts/fetch_release_notes.py
	$(PY) scripts/fetch_hcp_changelogs.py
	$(PY) scripts/fetch_blogs.py
	$(PY) scripts/fetch_events.py

plan: ## Recompute the content plan from data/ and state/ (data/deck-plan.json)
	$(PY) scripts/plan_deck.py

deck: ## Rebuild and verify the deck from the data already fetched (no network, no model)
	SKIP_FETCH=1 ENRICH=0 bash build.sh

verify: ## Re-run the gate on the last built deck
	$(PY) scripts/verify_build.py

summary: ## Re-print the summary of the last build
	$(PY) scripts/summarize_build.py

enrich: ## Ask the enrichment agent to word items that have no stored wording yet
	$(PY) scripts/enrich.py

test: ## Run the offline test suite (about half a minute)
	$(PY) -m unittest discover -s tests

template: ## Re-cut the template from a reference deck: make template DECK="path/to/deck.pptx"
	@test -n "$(DECK)" || { echo 'usage: make template DECK="path/to/reference deck.pptx"'; exit 2; }
	$(PY) scripts/make_template.py "$(DECK)"

# --- pipeline -----------------------------------------------------------------------------
preflight: ## Report what this machine can reach and which pipeline settings are present
	PIPELINE_MODE=preflight bash ci/run.sh

publish-check: ## Read-only: can the token in GIT_TOKEN see the repository's releases?
	$(PY) scripts/publish_release.py --check

build-image: ## Build the pipeline image
	docker build -t auto-pptx:latest .

docker-run: ## Run one weekly build in Docker (uses .env)
	docker compose run --rm auto-pptx

# --- housekeeping -------------------------------------------------------------------------
clean: ## Remove fetched data and scratch (keeps output decks and state/)
	rm -rf .work data/*.json data/*.tmp data/*.log
	@echo "cleaned fetched data + scratch (output/ decks and state/ kept)"

# --- legacy: update a full customer deck in place (superseded by `make build`) --------------
run-local: ## LEGACY: patch the automated slides of the .pptx in the repo root (needs Copilot CLI)
	bash run.sh

skills-check: ## Check whether the vendored pptx skill is up to date
	$(PY) .github/skills/skill-updater/scripts/check_updates.py --check

skills-update: ## Re-vendor out-of-date skills and bump the manifest
	$(PY) .github/skills/skill-updater/scripts/check_updates.py --update
