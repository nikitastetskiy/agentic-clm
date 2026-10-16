# Fonts — sources & usage

| File | Typeface | Source | License |
|------|----------|--------|---------|
| `Inter-Regular.ttf` | Inter 400, Latin subset | [Fontsource](https://fontsource.org/fonts/inter) build of [rsms/inter](https://github.com/rsms/inter) | SIL Open Font License 1.1 (`OFL.txt`) |
| `Inter-Bold.ttf` | Inter 700, Latin subset | same | same |

Inter is the deck's text typeface. These files are **not embedded in any deck** — the decks already
carry their own embedded copy. They are used only to *measure* text (`scripts/lib/textfit.py`): the
builder needs real glyph widths to predict where a line wraps, so that a release column or an event
card is filled to what fits instead of overflowing its box. Measuring with a bundled font keeps that
prediction identical on a laptop and in the pipeline container, whatever fonts are installed.

The Latin subset covers Basic Latin and Latin-1 (enough for the English and Western-European text the
deck shows); a glyph outside it is measured with the font's fallback width.
