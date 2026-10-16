# Working in this repository

auto-pptx builds a weekly HashiCorp product-updates PowerPoint deck from public sources. Read
`README.md` first; `docs/pipelines.md` covers how it runs unattended.

## Layout

- `build.sh` — the whole build: fetch → plan → enrich (optional) → build → verify → summary.
- `scripts/fetch_*.py` → `data/*.json` (generated). `scripts/plan_deck.py` → `data/deck-plan.json`.
  `scripts/build_deck.py` → `output/*.pptx`. `scripts/verify_build.py` is the gate.
- `scripts/lib/` — `slides.py` finds slide structures, `compose.py` writes paragraphs from template
  prototypes, `textfit.py` predicts text layout, `htmldoc.py` reads docs pages, `deck.py` handles slide
  order and roles.
- `config/deck.yaml` — the deck's slides by ROLE. `config/sources.yaml` — the public sources.
- `templates/cadence-template.pptx` — cut from a reference deck by `scripts/make_template.py`.
- `state/` — blog history and stored wording; updated by builds, tracked in git.
- `ci/` — the weekly and self-heal pipelines; `.github/workflows/` runs them on GitHub Actions,
  `.pipeline-config*.yaml` on IBM SPS. Both only call `ci/run.sh`.
- `run.sh`, `scripts/edit_*_slides.py`, `scripts/verify_deck.py`, `.github/agents/` — the LEGACY
  in-place updater. Not used by the weekly build; leave it alone unless asked.

## Commands

```bash
make test                          # offline suite, ~15 s — run it after every change
SKIP_FETCH=1 ENRICH=0 bash build.sh  # rebuild + verify from the data already fetched
make build                         # the full build with live fetches
python scripts/fetch_<x>.py        # one fetcher against its live source
```

## Rules

- **Slides are addressed by role, never by number.** Do not introduce slide numbers.
- **The build is deterministic.** No model call, clock read or network access in `plan_deck.py`,
  `build_deck.py` or `verify_build.py` (the date comes from `lib.common.today()`).
- **Formatting comes from the template.** New text is a clone of a template paragraph
  (`lib/compose.py`); never hard-code a font, size or colour.
- **Text must fit.** Anything written into a fixed box goes through `lib/textfit.py`.
- **Placement goes through `lib/layout.py`.** Never position a card, flag, icon, header or picture
  with ad-hoc offsets: the builder applies its rules and the gate's `layout` check enforces the same
  ones (repeated boxes on one line, icons 13.5 pt apart on one top line, nothing past the slide's
  edges, highlights of one entry apart). The template inherits the hand-made deck's small drifts;
  the rules exist to remove them, so do not copy a position from the template as "correct".
- **Public sources only.** Every line of the deck traces to a public HashiCorp or IBM page read by a
  fetcher. A CVE id may appear only if the public release notes for that release name it.
- **Never weaken the gate to make a deck pass.** A check that is wrong gets fixed narrowly, with a
  test; a deck that is wrong gets fixed in the fetcher, planner or builder.
- Match the surrounding code: standard library first, `xml.dom.minidom` for slide XML, a docstring
  that says why the module exists, comments for the non-obvious only.
- Every behaviour change needs a test in `tests/`; a parser change needs a small sample of the page
  structure under `tests/fixtures/html/`.
