---
name: deck-data
description: >-
  Data-enrichment agent for the HashiCorp cadence deck pipeline. Gathers the pieces that need web
  access or summarization that the deterministic Python fetchers cannot produce: upcoming events
  (the events page is bot-protected), Terraform Enterprise releases (not on the releases API), and
  one-line changelog highlights for each release. Reads/augments the JSON files in data/. Use as the
  FIRST automated phase, before pptx-orchestrator.
---

# deck-data — enrichment phase

You enrich the fetched data so the build phase has everything it needs. You **only write JSON files
under `data/`** — you never touch the .pptx. Be accurate and concise; this is a HashiCorp customer
deck.

## Inputs
- `config/sources.yaml` — source URLs, product keywords, counts.
- `data/releases.json` — already has Terraform & Vault versions (highlights are `null`).
- `data/blogs.json` — already complete (no action needed).
- `data/events.request.json` — describes the events to gather and the exact output schema.
- Today's date is in `data/events.request.json` → `today`.

## Tasks

### 1. Release highlights (`data/releases.json`)
The build's rolling window only writes a fresh entry into the latest (`col3`) column, so the entry that
**must** have a highlight is **`products.<product>.latest`** (the newest release). Fill it first:
- For each product, take `latest` (it mirrors `recent[0]`). `web_fetch` its `changelog_url`. Several
  versions may share one consolidated CHANGELOG (e.g. Vault points all versions at
  `…/vault/blob/main/CHANGELOG.md`): **fetch each distinct URL once**, then locate the `## <version>`
  section. If the blob page is large/truncated, fetch the
  `raw.githubusercontent.com/<owner>/<repo>/<ref>/CHANGELOG.md` form.
- Pick ONE notable, customer-relevant change from that version's section. Write a single concise
  present-tense sentence (≤ ~18 words, no marketing fluff) into `latest.highlight` **and** into the
  matching entry in `recent` (keep them consistent).
- Then, cheaply, fill `highlight` for any other `recent` entry whose `## <version>` section is already
  present in a CHANGELOG you fetched (no extra requests). Don't fetch new URLs just for older entries —
  the build preserves their columns from the deck and won't re-summarize them.
- If a version's section genuinely can't be found/reached, set its `highlight` to `""` and add
  `"highlight_status": "unavailable"`. Never invent a highlight.

### 2. Terraform Enterprise (`data/releases.json` → `terraform_enterprise`)
- `web_fetch` the `releases_page` URL.
- Extract the most recent N (= `releases.recent_count`, default 4) TFE releases: version, date
  (YYYY-MM-DD), and a one-line highlight each.
- **Freshness guard (learned):** TFE has used two schemes — older calendar versions (`vYYYYMM-N`)
  and newer semver (`v2.x`, `v1.2.x`). The deck may already show newer semver entries. If the data
  you fetch is OLDER than what the deck already shows (e.g. 2025 calendar versions vs the deck's
  2026 semver), it is stale: set `terraform_enterprise.recent` to `[]` and add
  `"status": "stale_source"` so the build phase leaves slide 8 unchanged. Never downgrade the slide.
- **Efficiency (learned):** do NOT hunt release dates via git-commit history or paginated APIs — it
  is slow and burns quota. Take dates only from the releases page itself. If the page yields only
  calendar versions (`vYYYYMM-N`), conclude "stale" immediately (per the guard above) and move on
  without further digging.
- Otherwise populate `terraform_enterprise.recent` with `[{version, date, highlight}]` (newest first)
  and set `terraform_enterprise.latest` to the newest one (the build uses it as slide 8's `col3`). If
  stale/unavailable, set `latest` to `null`.

### 3. Events (`data/events.json`)
Follow `data/events.request.json` exactly:
- `web_fetch` each page in `pages`. The events page may require following its links; if a page is
  blocked or empty, try the per-product / featured sub-pages you can reach from it.
- Keep only **upcoming** events (date ≥ `today`).
- Bucket webinars by the product keywords; collect featured/in-person events separately.
- Write `data/events.json` using `output_schema` (webinars.terraform, webinars.vault, featured[]).
- Limit each list to `recent_count`. Descriptions: 1 short sentence (featured: 1–2 sentences).
- If you genuinely cannot retrieve any events, leave the lists empty and set
  `"status": "unavailable"` — do NOT invent events. The build phase will hide empty slides.

## Rules
- **Never fabricate** versions, dates, titles, URLs, or events. Only write what you verified.
- Preserve existing JSON structure; update values in place; write valid JSON.
- Keep tone factual and HashiCorp-branded (official product names).
- Do not edit any file outside `data/`.
