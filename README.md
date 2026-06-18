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
| 18–22 | Featured / in-person **events** | HashiCorp events page |

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
   This takes roughly **30–45 minutes** — most of that is the assistant carefully researching the
   web (politely, with retries, because some HashiCorp pages rate-limit bots) and rebuilding the
   slides without breaking their formatting. It is meant to be a "kick it off and come back later"
   job, not an instant one.
3. **Open the result.** Look in the `output/` folder for the newest file, e.g.
   `CSE EMEA Customer Cadence Deck JUNE 2026 — 2026-06-18-1703.pptx`. Your original is untouched.
4. **Review it.** A plain-text report (`output/verify-*.txt`) and a written review
   (`output/review-*.md`) tell you what changed and confirm the style and slide-hiding are correct.
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
- *(Optional, for the visual review step)* LibreOffice + poppler — already included if you use Docker.

First-time setup:
```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
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
 ├─ 1. fetch (python)  fetch_releases.py · fetch_blogs.py · fetch_events.py  → data/*.json
 ├─ 2. copy            make_working_copy.py → output/<deck> — <timestamp>.pptx
 ├─ 2b. map (python)   map_slides.py → data/slide-structure.json (learned geometry; no re-discovery)
 ├─ 3. deck-data       agent: web_fetch events + TFE + changelog highlights → enrich data/*.json
 ├─ 3b. release edit (python)  edit_release_slides.py → applies the shift/refresh standard AND
 │                        date-reconciles mislabeled versions on slides 7,8,11,12; hard cases are
 │                        listed in data/release-edit-report.json for the agent (fallback_slides)
 ├─ 4. pptx-orchestrator  agent: edit fallback + blog slides (style-preserving), hide empties, repack
 ├─ 4b. verify (python)   verify_deck.py → output/verify-*.txt (deterministic gate: integrity, hide
 │                        audit, style, release-column latest, date-keyed accuracy) — reviewer's truth
 └─ 5. deck-reviewer   agent: judgment checks (data/highlights/links/scope); record learnings
```
The Python phases are fast and free; the **agent** phases (research, edit, review) are where the time
goes. Moving the release-slide edits into step 3b (deterministic Python) shrinks the slow build agent's
work to just the ambiguous cases, blogs, and slide-hiding.

### Skills (`.github/skills/`)
- **`pptx/`** — vendored from [anthropics/skills] at a pinned commit (see `.skill-manifest.json`).
  Provides the `unpack → edit → clean → pack` workflow that preserves all PowerPoint formatting.
- **`skill-updater/`** — checks whether the vendored pptx skill drifted from upstream before each
  run (`notify` by default; `update` to auto re-vendor). Keeps runs reproducible.
- **`hashicorp-deck-style/`** — the **mandatory** style guide: brand palette (`palette.json`),
  fonts (Inter/Poppins), per-slide layouts, date formats, tone, and a `style_check.py` guard that
  fails if a foreign colour/font is introduced.

### Agents (`.github/agents/`)
- **`deck-data`** — gathers what Python can't: events (bot-protected page), TFE releases (not on the
  API), and one-line changelog highlights. Writes only `data/*.json`; never invents anything.
- **`pptx-orchestrator`** — edits only the slides in `config/slide-map.yaml`, replacing text inside
  existing runs (never touching formatting), updating link targets, and hiding empty slides.
- **`deck-reviewer`** — verifies style preservation, data accuracy, links, and hide-don't-delete;
  writes `output/review-<stamp>.md` and records durable rules into the corrections log.

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
