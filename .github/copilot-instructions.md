# Copilot instructions — auto-pptx

These rules apply to **every** Copilot session in this repo (CLI window, coding agent, or the
pipeline's own sub-agents). They keep the assistant scoped to what it's meant to do: refresh a few
public-data slides of the HashiCorp **CSE EMEA Customer Cadence Deck** without ever breaking the
deck's hand-authored style or touching anything else.

## What this project does
A pipeline (`run.sh`, also `make run-local`) gathers the latest public HashiCorp data and writes a
**new, timestamped copy** of the deck with a handful of slides refreshed. Deterministic Python does
the predictable work (fetch + most slide edits); Copilot agents fill only the parts that need
judgement (event prose, ambiguous release columns, review). See `README.md` and the per-phase
comments in `run.sh` for the full flow.

## Golden rules (do not violate)
1. **Never modify the source deck.** Only ever edit the working copy under `output/` (made by
   `scripts/make_working_copy.py`). If no working copy path is given, make one — never open the
   `.pptx` in the repo root for editing.
2. **Only touch the slides in `config/slide-map.yaml`** (currently 7, 8, 9, 10, 11, 12, 13, 14,
   18–22). Customer slides, success plans, intro/branding — **off-limits**. When in doubt, leave it.
3. **Preserve the style exactly.** Follow the `hashicorp-deck-style` skill and edit *text inside
   existing runs only* — never change fonts, colours, sizes, or layout. Run its `style_check.py`
   guard before finishing. A foreign colour/font is a failure.
4. **Hide, don't delete.** Slides with no fresh data get `show="0"` on the slide root; the slide
   count must stay identical to the original. Never remove a slide.
5. **Never fabricate data.** Use only the facts in `data/*.json`. Leave a field empty rather than
   inventing a version, date, title, or link. Don't paste the raw `recent[]` API array into a
   release column — follow the shift/refresh standard in `config/slide-map.yaml`.
6. **Read `corrections/corrections.md` first** and apply every applicable rule (newer overrides
   older). When a human corrects the output, record it with
   `python scripts/capture_correction.py "<rule>" --category <cat> --slide <n> --source human`.

## Working with PowerPoint
- Always use the vendored **`pptx` skill** (`unpack → edit → clean → pack`) — never hand-write OOXML
  or use other libraries to repack. Don't re-vendor or "update" that skill unless explicitly asked.
- Use `data/slide-structure.json` (the geometry map) instead of re-discovering layout.
- When repointing a hyperlink, add a **fresh** relationship — never mutate a shared `rId`. The same
  rule applies to image relationships: to swap a picture, add the new media + a **fresh** image rel
  and repoint the picture's `<a:blip r:embed>` (see `add_image_rel` in `scripts/lib/pptx_edit.py`).
  Never overwrite an existing media file (rIds and media are frequently shared across slides).

## Prefer the deterministic path
Most slide edits are already done in Python (`scripts/edit_release_slides.py`,
`edit_blog_slides.py`, `edit_event_slides.py`). The build agent should edit **only** the slides those
reports list as skipped/fallback. Don't redo slides marked `handled`/`changed` — re-editing risks
regressions. Don't reach for `web_fetch` when a deterministic fetcher already produced the data.

## Running & finishing
- To run the pipeline, use `make run-local` / `run.sh` (or the documented `SKIP_*` flags). Don't
  invent a parallel workflow.
- **A full run takes ~30–40 min** (it calls three agents). When asked to run it from a chat window,
  launch it **detached in the background** (e.g. `mkdir -p output && bash run.sh >
  "output/run-$(date +%Y%m%d-%H%M%S).log" 2>&1 &`) so the window stays responsive. Rely on the
  completion notification instead of a blocking poll; tail `output/run-*.log` (and the per-phase
  `output/*.log`) to report progress, and print the summary once it finishes.
- Every run ends with `scripts/summarize_run.py`, which prints the table of changes and a "please
  review before sharing" notice. Keep that behaviour; the output is for a human to verify.
- Validate changes (`bash -n run.sh`, run the relevant script, or `make summary`) before declaring
  done. Don't add new linters/test frameworks.

## Safety
- Never commit secrets. `.env` is git-ignored; only `.env.example` is tracked.
- The deck is internal HashiCorp material — don't commit the `.pptx`, `data/`, or `output/` contents.
