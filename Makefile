# auto-pptx — convenience targets
.DEFAULT_GOAL := help
PY ?= python3

.PHONY: help fetch run run-local build-image docker-run skills-check skills-update summary clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'

fetch: ## Run the deterministic Python fetchers only (releases, blogs, events-prep)
	$(PY) scripts/fetch_releases.py
	$(PY) scripts/fetch_blogs.py
	$(PY) scripts/fetch_events.py

skills-check: ## Check whether the vendored pptx skill is up to date
	$(PY) .github/skills/skill-updater/scripts/check_updates.py --check

skills-update: ## Re-vendor out-of-date skills and bump the manifest
	$(PY) .github/skills/skill-updater/scripts/check_updates.py --update

run-local: ## Run the full pipeline locally (needs Copilot CLI authenticated)
	bash run.sh

build-image: ## Build the Docker image
	docker build -t auto-pptx:latest .

docker-run: ## Run the pipeline in Docker (uses .env)
	docker compose run --rm auto-pptx

summary: ## Re-print the action summary for the newest deck in output/
	$(PY) scripts/summarize_run.py

clean: ## Remove generated data and working scratch (keeps output decks)
	rm -rf .work data/*.json data/*.tmp
	@echo "cleaned scratch + fetched data (output/ decks kept)"
