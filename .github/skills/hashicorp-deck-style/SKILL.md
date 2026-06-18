---
name: hashicorp-deck-style
description: >-
  MANDATORY style guide for editing the HashiCorp CSE EMEA Customer Cadence Deck. Defines the brand
  palette, fonts, per-slide layouts, date formats, tone, and the strict "preserve existing
  formatting" rules. Use whenever ANY slide in this deck is edited so updates are visually
  indistinguishable from hand-authored slides. Includes a style-preservation checker.
user-invocable: true
---

# HashiCorp Cadence Deck — Style Skill

**Style preservation is mandatory.** Automated edits must be indistinguishable from the deck's
existing hand-authored slides. The safest edit replaces only the *text* inside an existing run and
changes nothing else.

## The Golden Rule

> **Edit the text of existing `<a:t>` runs. Never invent or alter formatting.**

When you replace content:
- Keep the surrounding `<a:rPr>` (color, font, size, bold) and `<a:pPr>` exactly as-is.
- Reuse the run that already styles that piece of content (e.g., the purple version number run).
- If you must add a paragraph (multi-item content), **copy `<a:pPr>`/`<a:rPr>` from a sibling**
  paragraph in the same shape — do not author new colors or fonts from memory.

## Brand assets (see `palette.json` for machine-readable values)

**Fonts** — Headers: `Poppins` (also `Inter Black`/`Inter SemiBold`). Body: `Inter` / `Inter Light`.
Do not introduce Aptos/Arial/Calibri into edited runs unless the original run already used them.

**Core colors**

| Use | Hex |
|-----|-----|
| Terraform (primary / brand) | `8B85FF` / `7B42BC` |
| Terraform variants | `7582FF` `6C81FF` `7D83FF` `C08DFF` |
| Vault (primary) | `F9B373` (variants `F9B571` `FBAA79`) |
| Accent coral / pink | `FF8791` |
| Tag chips (pastel) | `FFC2C8` `FDDEC5` `E5D0FF` `CDD5FF` |
| Text | `000000` / `1A1A1A` on light; `FFFFFF` on dark |

**Product color rule:** Terraform slides (7, 9, 10) use purple; Vault slides (11, 12, 13, 14) use
gold/orange. Never swap a product's accent color.

## Per-slide-type layout

### Release updates (7, 8, 11, 12)
- Month column headers (e.g. `June` / `May` / `April`) — update to the months actually covered.
- Each entry = a **version run** `vX.Y.Z (YYYY-MM-DD)` (styled in the product color) + a **one-line
  highlight** (body color). Keep the version-in-color + highlight-in-body pattern.
- Highlights: one concise sentence, present tense, ≤ ~18 words. Summarize a notable changelog item.
- Vault versions: base semver only (strip `+ent`, `+ent.hsm`, `+ent.fips`).
- Mark a pre-release entry exactly as the template does (e.g. "v1.16.0 - alpha … pre-release").

### Recent blogs (9, 13)
- Each entry: `<Month Day> – ` + the words **`See here`** (a hyperlink) + the blog title.
- Update the hyperlink **target URL in `slideN.xml.rels`** for each `See here` run — not just the text.
- Keep the topic tag chips (Speed & agility, AI, …) unchanged.

### Webinars (10, 14)
- Each entry: date + `WEBINAR` + `See here` (hyperlink) + title + short description.
- Only **upcoming** events (date ≥ run date). Update hyperlink targets in the `.rels`.

### Featured / in-person events (18–22)
- One event per slide: Title = event name; Subtitle = `Event | <date> | Register Here` (hyperlink);
  body = 1–2 sentence description.
- If there are fewer upcoming events than slides, **hide** the extra slides (`show="0"`), don't delete.
- Slide 22 is French (Paris) — keep its language if reused.

## Date formats
Match each slide's existing style. Releases use `(YYYY-MM-DD)`; blogs/webinars use `Month D`
(e.g. `Jun 2`). Mirror whatever the run you are replacing already uses.

## Tone & voice
HashiCorp product voice: clear, concise, technically precise, benefit-led. No marketing fluff, no
exclamation marks, no emoji. Use official product names ("HCP Terraform", "Terraform Enterprise",
"Vault Enterprise", "HCP Vault", "Vault Radar").

## Hyperlinks (important)
"See here" / "Register Here" links live as relationships. To change where a link points:
1. Find the run's `<a:hlinkClick r:id="rIdN"/>`.
2. In `ppt/slides/_rels/slideN.xml.rels`, update that relationship's `Target` to the new URL
   (external links keep `TargetMode="External"`).

## Before editing: read corrections
Always read `corrections/corrections.md` first and apply every applicable rule. These encode human
fixes from previous runs (this is how the agents "learn"). Newer rules override older ones.

## Style-preservation check
After editing, verify no foreign colors/fonts were introduced:
```bash
python .github/skills/hashicorp-deck-style/scripts/style_check.py unpacked/ 7 8 9 10 11 12 13 14 18 19 20 21 22
```
Exit 0 = clean; exit 1 = foreign hex/fonts found (review before packing).
