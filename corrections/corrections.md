# Corrections & Learnings

This file is the pipeline's memory. The **pptx-orchestrator reads it before every run** and applies
every applicable rule. This is how the agents "learn and re-adapt": when a human rectifies the
output, capture the lesson here (directly, or via `scripts/capture_correction.py`) and it will be
honored on the next run. Newer rules override older ones on conflict.

Keep rules **short, imperative, and general**. Group by category.

## How to add a correction
```bash
# Quick capture (also logs to corrections.jsonl):
python scripts/capture_correction.py "Vault webinar descriptions: max 1 line" --category events --slide 14 --source human
```
Or just add a bullet under the right heading below.

---

## Global
- Edit existing runs only — never change colors, fonts, sizes, or layout (style is mandatory).
- Hide slides with no fresh data (`show="0"`); never delete a slide.
- Never fabricate versions, dates, titles, URLs, or events.
- Use official product names: "HCP Terraform", "Terraform Enterprise", "Vault Enterprise", "HCP Vault", "Vault Radar".
- Deterministic slide geometry: scripts/map_slides.py extracts each automatable slide's editable shapes (role, x/y, text, hyperlinks) into data/slide-structure.json. run.sh Phase 2b regenerates it against the fresh working copy. Agents (esp. pptx-orchestrator) MUST consume it instead of dumping geometry themselves — this was the biggest time/quota sink.
- Deterministic post-build verification: scripts/verify_deck.py machine-checks the produced deck (integrity/slide-count vs original, hide audit via <p:sld show=0> on slide ROOTS, style_check, and that each release slide's latest/col3 column carries products.<p>.latest.version). run.sh Phase 4b runs it before the AI reviewer and writes output/verify-*.txt, which the reviewer reads as ground truth instead of eyeballing XML. This replaces ad-hoc manual XML checks and prevents the two mistakes seen on run 1558: the reviewer's false hide-FAIL (it checked presentation.xml) and a human mis-reading variable-entry columns as 'bloat'. The verifier also prints entries-per-column so the hand-curated multi-entry layout is self-evident.

## Releases (slides 7, 8, 11, 12)
- Vault version numbers: base semver only — strip `+ent`, `+ent.hsm`, `+ent.fips` (e.g. `v1.21.8`).
- Show stable releases only; never list alpha/beta/rc as a current release (a clearly-labelled
  pre-release callout is fine where the template already has one).
- Highlights: one concise present-tense sentence, ≤ ~18 words, benefit-led, no marketing fluff.
- Terraform Enterprise releases.json uses calendar versions (vYYYYMM-N) and can lag the deck's 2026 semver entries; verify TFE source freshness and never downgrade slide 8 to older data  _(slide 8)_
- When Vault release highlights are unavailable, leave slides 11/12 unchanged and flag for manual highlight entry; never pair new versions with stale/blank highlights  _(slide 11)_
- Vault changelog_url must point to the PUBLIC hashicorp/vault main CHANGELOG (blob/main/CHANGELOG.md); the releases API url_changelog points at the private vault-enterprise repo and per-version public tags don't all exist. Agent fetches the consolidated changelog once and greps the '## <version>' section for each highlight.
- HCP Vault (slide 12) tracks the same Vault core versions as Vault Enterprise (slide 11); when 11 gains a new June release and 12 has no June column, flag 12 for manual structural update rather than silently shipping it a release behind  _(slide 12)_
- Release slides (7,8,11,12) are a ROLLING 3-column window driven by products.<product>.latest in releases.json, NOT a full rewrite. Each column is a (month-header, entry) pair; col3 is always the latest. Rule: if latest.month_label == current col3 header month, refresh col3's entry in place and leave col1/col2 untouched; if latest's month is newer, shift the (header,entry) pairs one column left (col1<-col2, col2<-col3, oldest drops) and write latest into col3. NEVER blank or rebuild all 3 columns from the API (it caps at 20 items; for Vault all same-month backports, so col1/col2 history can't be re-derived and must be preserved). Slides 11 (Vault Ent) and 12 (HCP Vault) use the same vault.latest but apply the rule independently against their own columns (12 may lag) — goal is neither stays a month behind; they need NOT be byte-identical. The build reads data/slide-structure.json (from scripts/map_slides.py, regenerated each run in run.sh Phase 2b) for exact shape roles/geometry so it never re-discovers layout.
- RETRACTED (was "COLUMN BLOAT FAILURE", run 1558 — the diagnosis was WRONG). The original deck's release columns are HAND-CURATED and already contain a VARIABLE number of version entries per column (verified in the original: slide 7 col2/May lists 5 patches v1.15.4..v1.15.1; slide 7 col1/April lists 2; slide 11 col1 March/April lists 3 incl. v2.0.0 with sub-bullets). The build did NOT bloat anything: it correctly preserved col1/col2 verbatim and only refreshed col3 with products.<product>.latest. CORRECT STANDARD: a column may hold 1+ entries; NEVER collapse a multi-entry column to one; refresh col3 (or shift on a new month) and leave col1/col2 byte-identical. recent_count is irrelevant to the build (it renders `latest`, not recent[]).
- Release-column version runs can SHARE one hyperlink rId with another entry: slide 11 col3 v2.0.2 and col2 v2.0.1 both use rId6 -> tag/v2.0.1, so the v2.0.2 'tag' link actually points at v2.0.1 (pre-existing in the source deck). Verify each version run's hlinkClick rId resolves to ITS OWN vX.Y.Z tag; when refreshing/adding an entry, give it its own rel rather than reusing a shared one (the build correctly minted rId8->v2.0.3).  _(slide 11)_
- A release's authoritative identity is its DATE, not the printed version string: original decks can mislabel a version (e.g. slide 7 showed '1.15.4 (2026-05-27)' but that date is really 1.15.5, and even its hyperlink pointed at v1.15.5). Reconcile every dated entry against releases.json keyed by exact date; the API data wins, the hyperlink is corroborating-only (never the sole basis for a change). This is now enforced deterministically by scripts/edit_release_slides.py (reconcile pass) and guarded by scripts/verify_deck.py Check 5 (release_accuracy).  _(slide 7)_
- Release slides 7,8,11,12 are edited deterministically by scripts/edit_release_slides.py BEFORE the build agent; it writes data/release-edit-report.json with fallback_slides[] (ambiguous, agent handles) and handled_slides[] (done, agent must not re-touch). When fixing a release-slide hyperlink, mint a FRESH relationship id (rIds are often SHARED across entries — mutating a shared rel's Target corrupts a sibling entry's link).

## Blogs (slides 9, 13)
- Update the `See here` hyperlink target in the slide `.rels`, not just the visible text.
- Keep the topic tag chips (Speed & agility, AI, …) unchanged.
- Blog slides may carry pre-existing entries BELOW the refreshed newest-first top list that are not in blogs.json and may sit out of strict date order (slide 13 'June 4 - Rethinking infrastructure access' renders under May entries). The build only refreshes the top of the list from blogs.json; do not flag these lower curated/out-of-order entries as build errors.  _(slide 13)_

## Events / Webinars (slides 10, 14, 18–22)
- Only include events whose date is today or later.
- Featured events 18–22: one event per slide; hide extras when there are fewer events than slides.
- Slide 22 is a French-language (Paris) event — keep locale/language if it is reused.
- Verify the hide step by checking `<p:sld ... show="0">` on each SLIDE-ROOT file (ppt/slides/slideN.xml), resolving display position -> file via presentation.xml + rels (use `python scripts/hide_slides.py UNPACKED --list`). Do NOT count `show` on `<p:sldId>` in ppt/presentation.xml — PowerPoint's "Hide Slide" sets the attribute on the slide root, not on sldId, so checking presentation.xml yields a FALSE "zero hidden" (this caused a bogus FAIL on run 1558, whose 10/14/18–22 WERE correctly hidden). Also: a hidden slide RETAINS its text, so seeing past-dated text in slideN.xml is NOT evidence the slide is visible — check the show attribute.
- When events.json status == 'unavailable' (e.g. HTTP 429, not a confirmed-empty fetch), pick ONE policy and make build notes match the deck: either hide event slides 10/14/18-22 (show="0") or leave them unchanged — but never ship past-dated (< today) webinars/events as visible.

## Style
- Terraform slides use purple (`8B85FF` / `7B42BC`); Vault slides use gold/orange (`F9B373`).
  Never swap a product's accent color.
- Reviewer: diff a:t text via XML DOM (getElementsByTagName('a:t')), not a <a:t>...</a:t> regex; deck-wide xml:space=preserve repairs and <a:tab> elements cause false text-diff positives on out-of-scope slides
