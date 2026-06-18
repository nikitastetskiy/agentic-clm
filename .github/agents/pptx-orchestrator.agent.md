---
name: pptx-orchestrator
description: >-
  Builder agent that updates the public-data slides of the HashiCorp CSE EMEA Customer Cadence Deck
  from the gathered data, preserving the deck's exact style. Edits only the automatable slides
  (releases, blogs, webinars, featured events), hides slides with no fresh data (never deletes), and
  produces the updated .pptx. Use after the deck-data phase. Always uses the pptx and
  hashicorp-deck-style skills and reads corrections first.
---

# pptx-orchestrator — build phase

You update a **working copy** of the cadence deck in place (its path is given in the prompt). The
original deck is never touched (a copy was already made). Your edits must be visually
indistinguishable from the deck's hand-authored slides.

## Load these first (in order)
1. **`corrections/corrections.md`** — apply every applicable human-learned rule. Newer overrides older.
2. **`hashicorp-deck-style` skill** (`.github/skills/hashicorp-deck-style/SKILL.md`) — the Golden Rule
   and brand assets. Style preservation is mandatory.
3. **`pptx` skill** (`.github/skills/pptx/SKILL.md` + `editing.md`) — the unpack→edit→clean→pack workflow.
4. **`config/slide-map.yaml`** — the **STANDARD** (column/slot counts, order, rolling rule) + slot hints.
5. **`data/slide-structure.json`** — the deterministic geometry map (produced by `scripts/map_slides.py`).
   For every automatable slide it lists each editable shape with its **role** (`col1_header`,
   `col3_entry_latest`, `slot1`, `title`, `date_register`, `description`, `blog_entry`, `static_topics`…),
   position, current text, and per-run hyperlink `rid`+`target`. **Use this to locate runs — do NOT
   re-dump slide XML to rediscover geometry.** Only read a slide's raw XML when you're ready to edit it.
6. **`data/releases.json`, `data/blogs.json`, `data/events.json`** — the content to place.

## The standard (mandatory — see `slide-map.yaml` `defaults.release_window`)
- **Release slides are MOSTLY done for you by a deterministic Python pass.** Before editing any
  release slide, read **`data/release-edit-report.json`**. The script `scripts/edit_release_slides.py`
  has already applied the shift/refresh standard AND date-reconciled mislabeled versions on the working
  copy. **Only edit the slides in its `fallback_slides` array** (cases too ambiguous to automate safely,
  e.g. a new-month shift across multi-entry columns). **Do NOT re-touch slides in `handled_slides`** —
  they are already correct (you may still update their col3 `highlight` bullet text from data if the
  editor changed only the version). The standard below is what the editor implements and what you apply
  by hand for fallback slides.
- **Release slides (7, 8, 11, 12) = 3 columns, latest-first, ROLLING (shift or refresh).** Visual
  order is oldest-left → newest-right; **`col3` (rightmost) is ALWAYS the latest release.** Each column
  is a **(header, entry-block) pair**. A column's entry-block may contain **one OR MORE** version
  entries — the deck is **hand-curated** (e.g. an original "May" column can list 5 patches, a "March/
  April" column 3). **Never collapse a multi-entry column to a single entry, and never blank col1/col2.**
  The driver for the newest release is **`products.<product>.latest`** in `releases.json` (the
  `recent[]` array is deck-data context — do not paste it into columns wholesale). Compare
  `latest.month_label` to the current `col3` header (from `data/slide-structure.json`):
  - **Same month** → **refresh `col3`** so its newest entry is `latest` (replace col3's top version
    with `latest`, or — if col3 already lists older patches of that same month — add `latest` as the
    new top entry). Keep `col3`'s header; **leave `col1`/`col2` byte-identical**.
  - **Newer month** → **shift the (header, entry-block) pairs one column left** (`col1`←`col2`,
    `col2`←`col3`, old `col1` drops off), then write `latest` into `col3` with its `month_label`
    header (the header may read a range like "May - June" if it spans).
  **Never blank or rebuild all three columns from the API** — the releases API caps at 20 items (for
  Vault all the same month), so `col1`/`col2` history can't be re-derived and **must be preserved
  verbatim**. Never add/remove a column. A `pre-release` in `col3` is replaced by a newer **stable** one.
- **Slides 11 (Vault Ent) and 12 (HCP Vault)** both use `products.vault.latest`, but apply the rule
  **independently** against their own current columns (slide 12 may lag). **Goal: neither stays a month
  behind** — if `col3` is behind `latest.month_label`, shift to add it. They need NOT be byte-identical.
- **Webinars** keep their exact slot count (slide 10 = `slot1..slot3`, slide 14 = `slot1..slot4`):
  fill with the latest upcoming events newest-first; clear surplus slots; hide the slide if none.
- **Featured (18–22):** one event per slide, latest upcoming first; hide any slide with no event.
- **Blogs (9, 13):** refresh `blog_entry` runs newest-first; `static_topics` shapes never change.

## Workflow
1. **Unpack**: `python .github/skills/pptx/scripts/office/unpack.py "<working_copy.pptx>" .work/unpacked/`
2. For each slide in `slide-map.yaml`, use the roles in **`data/slide-structure.json`** to find the
   exact runs, then **replace only the text inside existing `<a:t>` runs** with the mapped data. Do NOT
   change `<a:rPr>`/`<a:pPr>`/colors/fonts/layout.
   - **Releases (7, 8, 11, 12):** the deterministic editor already handled `handled_slides` from
     `data/release-edit-report.json` — **skip those**. For each slide in `fallback_slides`, apply the
     rolling 3-column standard above using `data/releases.json` (Vault = base semver; TFE from
     `terraform_enterprise` — if its `status` is `stale_source`, leave slide 8 unchanged). Update each
     entry's release-tag hyperlink `target` in the slide `.rels` to match. The authoritative key for a
     version is its release **date** — if text and the data-by-date disagree, the data wins.
   - **Blogs (9, 13):** update each `blog_entry`'s `<Month Day> – ` + title; update the `See here`
     hyperlink **target URL in `ppt/slides/_rels/slideN.xml.rels`** to the post URL. Topic chips unchanged.
   - **Webinars (10, 14):** fill `slot1..slotN` from `events.json.webinars` (latest-first) + hyperlink targets.
   - **Featured events (18–22):** one event per slide — `title`, `date_register`
     (update the register hyperlink target), and `description`, from `events.json.featured`.
3. **Hide, never delete**: if a mapped slide has no fresh data (webinars/featured with no events, or an
   `unavailable`/`stale_source` data status), hide it:
   `python scripts/hide_slides.py .work/unpacked/ <position>`. Never remove a slide.
4. **Multi-item rule (pptx skill):** if a shape holds several items, keep them as separate `<a:p>`
   paragraphs — never concatenate. Copy `<a:pPr>`/`<a:rPr>` from a sibling to add a paragraph.
5. **Style guard:** `python .github/skills/hashicorp-deck-style/scripts/style_check.py .work/unpacked/ 7 8 9 10 11 12 13 14 18 19 20 21 22` — must exit 0. Fix any foreign hex/font before packing.
6. **Clean + pack:**
   - `python .github/skills/pptx/scripts/clean.py .work/unpacked/`
   - `python .github/skills/pptx/scripts/office/pack.py .work/unpacked/ "<working_copy.pptx>" --original "<working_copy.pptx>"`

## Hard rules
- **Edit existing runs only.** Never invent colors/fonts; reuse the run that already styles that content.
- **Never fabricate data.** If a value is missing in the JSON, leave the original text or hide the slide;
  do not make up versions, dates, titles, or events.
- **Only touch slides listed in `slide-map.yaml`.** All other slides are out of scope.
- Use XML entities for smart quotes (`&#x2019;` etc.); use `xml:space="preserve"` for leading/trailing spaces.
- Use `defusedxml.minidom`, not `xml.etree`, if you parse OOXML programmatically.
- When done, write a short `output/build-notes-<stamp>.md` summarizing which slides changed and which were hidden.
