# auto-pptx — Automated HashiCorp Cadence Deck Updater

Keep the recurring **CSE EMEA Customer Cadence Deck** up to date **without hand-editing slides
every month**. You point this tool at your PowerPoint, it gathers the latest public HashiCorp
information (product releases, blog posts, webinars and events), and it writes you a **new copy of
the deck** with those slides refreshed — in the deck's own fonts, colours and layout.

It's powered by the **GitHub Copilot CLI** with a few purpose-built "skills" and "agents", plus some
plain Python for the boring, predictable bits.

> 🔒 **Your original file is never touched.** Every run saves a brand-new, timestamped copy in the
> `output/` folder. Slides that have no fresh information are **hidden, not deleted** — so nothing is
> ever lost.

---

## Quickstart

```bash
# 1. Get the code and enter it
git clone <this-repo-url> auto-pptx && cd auto-pptx

# 2. One-time setup (Python deps + the headless browser used for the events page)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium

# 3. Authenticate the GitHub Copilot CLI (needed for the AI phases)
#    Install: https://github.com/github/copilot-cli   then:
copilot            # sign in once when prompted (or set COPILOT_GITHUB_TOKEN — see .env.example)

# 4. Put your deck in the project root (any single .pptx), then run the whole pipeline
make run-local

# 5. Open the newest file in ./output/  — your original is untouched.
```

That one command fetches the latest data, makes a timestamped copy, updates the automatable slides,
and writes a verification report. A full run takes **~30–40 minutes** (almost all of it the AI
edit/review phases; the data-gathering is now just seconds). Prefer to run unattended on a schedule?
See [Running it unattended](#running-it-unattended-docker--scheduled).

Useful variants:
```bash
make help                              # list every command
make fetch                             # just refresh data/*.json (seconds, no Copilot credits)
make summary                           # re-print the change table for the newest deck in output/
SKIP_DATA=1 SKIP_REVIEW=1 make run-local   # run only part of the pipeline (see run.sh for flags)
SOURCE_PPTX="/path/to/Deck.pptx" make run-local   # use a deck that lives outside the repo
```

### Run it from a fresh Copilot window (the friendly way)

Prefer a chat window over `make`? A fresh `copilot` session in this repo
**auto-loads** the scope guardrails (`.github/copilot-instructions.md`) and the
three custom agents — so the easiest way to run everything is to just *ask
Copilot to run it*:

```bash
cd auto-pptx
source .venv/bin/activate     # Playwright (events + photos) lives in the venv
copilot
```
Then, inside the window:
```text
/env             # confirm it loaded the instructions + the 3 agents
/allow-all       # let it run the pipeline's shell + file edits without prompting
> Run the full cadence deck pipeline in the background (bash run.sh),
  tail the log for progress, and show me the summary when it's done.
```
That reproduces a complete run — `run.sh` calls the `deck-data`,
`pptx-orchestrator`, and `deck-reviewer` agents internally with their tuned
prompts. Because a full run takes ~30–40 min, the assistant launches it
**detached** (logging to `output/run-*.log`) so the window stays responsive: it
returns control immediately, reports progress by tailing the log, and is
**notified on completion** instead of sitting in a blocking wait. The finished,
timestamped copy lands in `./output/` (your original is untouched), and the run
ends by printing the action-summary table and a "please review before sharing"
notice.

Power-user variant: run the fast deterministic phases yourself
(`make fetch && make event-images`), then drive a single agent with `/agent`
(or `copilot --agent pptx-orchestrator --allow-all -p "…"`) for a one-off edit.
Either way, **launch from the repo root with `.venv` active** so the agents,
guardrails, and headless browser are all available.

---

## In plain English: what does it actually do?

Once a month (or whenever you like) you would normally open the deck and manually:
- update the **Terraform / Vault release** slides with the newest versions,
- swap in the **latest blog posts**,
- refresh the **upcoming webinars and events**.

This tool does that for you and hands back a finished copy to review. You stay in control: you open
the result, check it, and tweak anything you don't like. If you correct something, the tool
**remembers** so it does it your way next time.

### What it updates for you

| Slides | What | Where the info comes from |
|--------|------|---------------------------|
| 7, 11, 12 | Terraform / Vault (Enterprise + HCP) **release** updates | HashiCorp releases API |
| 8 | **Terraform Enterprise** releases | HashiCorp releases page |
| 9, 13 | Terraform / Vault recent **blogs** | HashiCorp blog feed |
| 10, 14 | Terraform / Vault upcoming **webinars** | HashiCorp events page |
| 18–22 | Featured / in-person **events** (title, date, 2–3 bullet description, photo) | HashiCorp events page |

Everything else — customer-specific slides, success plans, intro/branding slides — is **left exactly
as you made it**. This tool only touches the slides listed above.

---

## How you'd typically use it (the monthly routine)

1. **Drop in the latest deck.** Put your current `.pptx` in the project folder (or keep the one
   that's already there).
2. **Run one command.**
   ```bash
   make run-local
   ```
   This takes roughly **30–40 minutes** — almost all of it the assistant carefully rebuilding the
   slides and reviewing them without breaking formatting. (Data gathering used to dominate the run;
   it's now deterministic Python that finishes in seconds.) It's a "kick it off and come back later"
   job, not an instant one.
3. **Open the result.** Look in the `output/` folder for the newest file, e.g.
   `CSE EMEA Customer Cadence Deck JUNE 2026 — 2026-06-18-1703.pptx`. Your original is untouched.
   The run ends by printing a **summary table of everything it changed** (which slides, what action)
   plus a clear *"please review before sharing"* notice with the exact path to your new deck — so you
   always know what to double-check. Re-print it anytime with `make summary`.
4. **Review it.** The printed summary, a plain-text report (`output/verify-*.txt`) and a written
   review (`output/review-*.md`) tell you what changed and confirm the style and slide-hiding are
   correct.
5. **Correct anything you don't like — once.** Record the fix so it becomes a permanent rule:
   ```bash
   python scripts/capture_correction.py "Vault webinar descriptions: keep to one line" \
       --category events --slide 14 --source human
   ```
   Next run, the assistant reads your corrections first and follows them. That's the "it learns"
   part.

That's the whole loop: **run → review → (optionally) correct → run again**.

> 💡 Just want the raw data without running the full assistant? `make fetch` refreshes the
> `data/*.json` files (releases, blogs) in a few seconds and uses no Copilot credits.

---

## What you need

- **GitHub Copilot CLI** (and a Copilot subscription). A fine-grained token with the
  **"Copilot Requests"** permission for unattended/Docker runs.
- **Python 3.11+** and the packages in `requirements.txt`.
- *(For the upcoming-events slides)* a one-time Playwright Chromium install (the events page is
  behind a bot-check that only a real browser passes). The same browser is reused to screenshot each
  event's leadspace hero (the gradient banner) for the featured-event photos. Without it, events fall
  back to the agent and existing slide photos are kept.
- *(Optional, for the visual review step)* LibreOffice + poppler — already included if you use Docker.

First-time setup:
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium   # enables the events fetcher
```

---

## Running it unattended (Docker / scheduled)

If you want this to run by itself on a schedule (say, monthly), use the Docker image:
```bash
cp .env.example .env        # add COPILOT_GITHUB_TOKEN (fine-grained PAT, "Copilot Requests")
make build-image
make docker-run             # one self-contained run; writes a new deck to ./output/
```
Each run is one-shot and repeatable (always a fresh timestamped copy), so you can drive
`make docker-run` from cron, launchd, or CI on whatever cadence you want.

You can also run just part of the pipeline:
```bash
SKIP_DATA=1 SKIP_REVIEW=1 bash run.sh      # fetch + copy + build only
```

---

## ⚠️ Before you publish this repository

This project is safe to share, but **the deck itself is internal HashiCorp material**. Before
pushing to GitHub:

- **Don't commit the customer deck** to a public (or broadly shared) repository. See
  "Bring your own deck" below — keep the `.pptx` out of git and have each user drop in their own.
- **Don't commit secrets.** `.env` is already git-ignored; only `.env.example` (no real values) is
  tracked. Double-check before your first push.
- `data/` and `output/` (fetched data and generated decks) are git-ignored too, so you won't leak
  generated copies.

### Bring your own deck
The tool automatically picks up the single `.pptx` in the project root, or you can point at one
explicitly:
```bash
SOURCE_PPTX="/path/to/your/Cadence Deck.pptx" make run-local
```
This lets you keep the actual deck **out of the repository** while still sharing the automation.

---

## How it works under the hood

```
run.sh
 ├─ 0. skill-updater   check the vendored pptx skill against upstream (advisory)
 ├─ 1. fetch (python)  fetch_releases.py · fetch_changelogs.py (highlights) · fetch_tfe_releases.py
 │                        (TFE + freshness guard) · fetch_blogs.py · fetch_events.py (Playwright) → data/*.json
 ├─ 1b. event images (python) fetch_event_images.py (Playwright) → screenshots the live event
 │                        leadspace hero (gradient bg) at 680px width, plus an on-brand HashiCorp
 │                        series-hero fallback for events with no per-page hero → data/event-images/
 ├─ 2. copy            make_working_copy.py → output/<deck> — <timestamp>.pptx
 ├─ 2b. map (python)   map_slides.py → data/slide-structure.json (learned geometry; no re-discovery)
 ├─ 3. deck-data       agent: event descriptions + light highlight polish (NO changelog/TFE web_fetch)
 ├─ 3b. release edit (python)  edit_release_slides.py → applies the shift/refresh standard AND
 │                        date-reconciles mislabeled versions on slides 7,8,11,12; hard cases are
 │                        listed in data/release-edit-report.json for the agent (fallback_slides)
 ├─ 3c. blog edit (python)     edit_blog_slides.py → refreshes the newest posts on slides 9,13
 ├─ 3d. event edit (python)    edit_event_slides.py → fills webinars (10,14) + featured events
 │                        (18–22) with 2–3 description bullets + shrink-to-fit titles, swaps each
 │                        event photo for its captured leadspace screenshot, clears surplus slots,
 │                        hides slides with no event
 ├─ 4. pptx-orchestrator  agent: only the leftover/ambiguous release cases + anything the editors
 │                        skipped (style-preserving), hide empties, repack
 ├─ 4b. verify (python)   verify_deck.py → output/verify-*.txt (deterministic gate: integrity, hide
 │                        audit, style, release latest, date-keyed accuracy, blog + event coverage)
 ├─ 5. deck-reviewer   agent: judgment checks (data/highlights/links/scope); record learnings
 └─ 6. summarize (python) summarize_run.py → prints the action table + "please review" notice
```
The Python phases are fast and free; the **agent** phases (research, edit, review) are where the time
goes. Moving the release, blog, and event edits into steps 3b–3d (deterministic Python) shrinks the
slow build agent's work to just the rare ambiguous cases — most runs need no slide edits from it.

### Skills (`.github/skills/`)
- **`pptx/`** — vendored from [anthropics/skills] at a pinned commit (see `.skill-manifest.json`).
  Provides the `unpack → edit → clean → pack` workflow that preserves all PowerPoint formatting.
- **`skill-updater/`** — checks whether the vendored pptx skill drifted from upstream before each
  run (`notify` by default; `update` to auto re-vendor). Keeps runs reproducible.
- **`hashicorp-deck-style/`** — the **mandatory** style guide: brand palette (`palette.json`),
  fonts (Inter/Poppins), per-slide layouts, date formats, tone, and a `style_check.py` guard that
  fails if a foreign colour/font is introduced.

### Agents (`.github/agents/`)
- **`deck-data`** — fills the prose Python can't and lightly polishes the pre-fetched release
  highlights from a local changelog excerpt (no web access). Release highlights and Terraform
  Enterprise releases are now fetched **deterministically** in step 1, so this agent mostly writes
  short event descriptions/locations. Writes only `data/*.json`; never invents facts.
- **`pptx-orchestrator`** — edits only the slides in `config/slide-map.yaml`, replacing text inside
  existing runs (never touching formatting), updating link targets, and hiding empty slides.
- **`deck-reviewer`** — verifies style preservation, data accuracy, links, and hide-don't-delete;
  writes `output/review-<stamp>.md` and records durable rules into the corrections log.

### Guardrails (`.github/copilot-instructions.md`)
Every Copilot session in this repo (the CLI window, the coding agent, or the pipeline's own
sub-agents) reads this file first. It pins the scope so the assistant **only** edits the automatable
slides, never touches the source deck, hides-rather-than-deletes, preserves the deck's style, and
never fabricates data. If you drive the deck interactively in the Copilot window instead of via
`run.sh` (see [Run it from a fresh Copilot window](#run-it-from-a-fresh-copilot-window-the-friendly-way)),
these are the rules it follows.

### The learning loop (`corrections/`)
The orchestrator **reads `corrections/corrections.md` before every run** and applies each rule. The
reviewer also records recurring issues automatically. Newer rules override older ones — so the tool
gets more "you" over time.

---

## Configuration
- `config/sources.yaml` — source URLs, product keywords, item counts, retry policy.
- `config/slide-map.yaml` — which slide maps to which data, plus slot hints. **This is the one place**
  to add or adjust which slides get automated.

## Roadmap / not yet wired
- **Google Drive delivery** — placeholders exist in `.env.example` (`GDRIVE_*`). When you decide on
  unattended scheduling with a Drive account, the upload step plugs in after the review phase.

## Licensing note
The vendored `pptx` skill is **proprietary to Anthropic** — see `.github/skills/pptx/LICENSE.txt`.
Review its terms before redistribution.

[anthropics/skills]: https://github.com/anthropics/skills
