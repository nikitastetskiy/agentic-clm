# Copilot instructions — auto-pptx

> **Legacy.** These guardrails belong to the earlier in-place updater (`run.sh`: copy a full customer
> deck and patch its automated slides by number). The project's current path is the **weekly build**
> (`build.sh`), which builds a deck of only the automated slides from a template — see `README.md` and
> `AGENTS.md`. Follow those for any work on the weekly build.

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
4. **Hide, don't delete.** Slides with no fresh data get `show="0"` on the slide root; **never remove a
   slide** and the count must never shrink. It MAY grow: the featured-event section auto-grows by
   duplicating its template (one slide per extra upcoming event, no cap), so `verify_deck` allows
   `count >= original` (a lower count means a deletion and fails).
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
- **The deterministic gate decides whether the agents run.** After the Python editors, `run.sh`
  runs `verify_deck.py` (Phase 3e); if it PASSES, the build (`pptx-orchestrator`) and review
  (`deck-reviewer`) agents are skipped automatically — that's expected, not an error. They only run
  when the gate finds a real gap, or when `FORCE_AGENTS=1` is set. Don't force the agents to run when
  the gate is green.
- **Never finish on a verify FAIL — loop until it passes.** A `verify_deck` `OVERALL: FAIL` is never an
  acceptable end state. When the gate (Phase 3e) or the post-build re-verify (Phase 4b) fails, you MUST:
  (1) **inspect** each failing check in `output/verify-*.txt` and trace it to a root cause; (2) **fix**
  that root cause — at the deterministic layer (`scripts/*.py`) when it's a logic/allocation bug the
  agents can't touch, or in `data/*.json` / the slide when it's content; (3) **record the learning** with
  `scripts/capture_correction.py`; (4) **re-run** and re-verify — for an isolated code/data fix reuse the
  same inputs (`SKIP_FETCH=1 SKIP_DATA=1`) so the fix is the only variable and the loop stays fast. Repeat
  until `OVERALL: PASS`. Two hard limits: **never fabricate data or weaken/bypass a check** just to turn it
  green (that defeats the gate and violates "never fabricate"); and if a check still fails after a few
  honest attempts because of a genuine upstream gap (e.g. a source truly lacks the data), **stop and
  escalate to the human** with the specific blocker rather than shipping a FAIL or looping forever.
- **A full run takes ~5–10 min** when the deterministic gate passes (build/review agents are skipped); a little longer if the agents also run. When asked to run it from a chat window,
  start it with the shell tool's **async/background** mode (logging to `output/run-*.log`) and then
  **stop** — the CLI notifies you automatically when it finishes. **Never `sleep`/`tail`-poll to
  babysit it** (busy-wait; wastes time + credits). Print the summary when the completion
  notification arrives. If the user wants live progress, they can `tail -f output/run-*.log`.
- Every run ends with `scripts/summarize_run.py`, which prints the table of changes and a "please
  review before sharing" notice. Keep that behaviour; the output is for a human to verify.
- Validate changes (`bash -n run.sh`, run the relevant script, or `make summary`) before declaring
  done. Don't add new linters/test frameworks.

## Safety
- Never commit secrets. `.env` is git-ignored; only `.env.example` is tracked.
- The deck is internal HashiCorp material — don't commit the `.pptx`, `data/`, or `output/` contents.
