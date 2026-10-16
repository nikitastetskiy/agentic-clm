"""Predict how text lays out in a slide text box, so the builder fills a box to what FITS.

PowerPoint does not reflow a deck when it opens one: a text box whose content is too long simply
runs past its border. A deck built unattended therefore has to know, before it writes a column or a
card, how many lines each paragraph will take. This module measures text with the deck's own typeface
(Inter, bundled under assets/fonts/ — see its CREDITS.md) and applies the same greedy word wrap
PowerPoint uses.

The model is calibrated against PowerPoint itself, using boxes in the reference deck that PowerPoint
sized to their text ("resize shape to fit text"): their heights give the line pitch it uses for Inter
(1.21-1.22 x the font size, for every weight), and the blog-title boxes show where it wraps a line —
a title measuring 99.0 % of the column still sat on one line, one measuring 106.6 % took two.
tests/test_textfit.py re-checks both against the template. SAFETY keeps the estimate on the
pessimistic side of that: one line too many costs a slightly shorter highlight, one line too few
costs text hanging out of its box.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from functools import lru_cache

from lib.common import REPO_ROOT

FONT_DIR = REPO_ROOT / "assets" / "fonts"
FACES = {False: "Inter-Regular.ttf", True: "Inter-Bold.ttf"}
REF_SIZE = 1000         # measure at 1000 units and scale: widths are linear in font size
LINE_FACTOR = 1.22      # height of one single-spaced line of Inter, per point of font size
SAFETY = 1.01           # widths are inflated by this much before wrapping
EMU_PT = 12700
DEFAULT_INSET = 91440   # a text box's default left/right/top/bottom inset (0.1 in), in EMU


@lru_cache(maxsize=None)
def _font(bold: bool):
    try:
        from PIL import ImageFont
        return ImageFont.truetype(str(FONT_DIR / FACES[bold]), REF_SIZE)
    except Exception:  # noqa: BLE001 — Pillow or the font file missing: fall back to an average width
        return None


@lru_cache(maxsize=20000)
def text_width(text: str, size_pt: float, bold: bool = False) -> float:
    """Width of `text` on one line, in points."""
    font = _font(bold)
    if font is None:
        return len(text) * size_pt * (0.50 if bold else 0.48)
    return font.getlength(text) * size_pt / REF_SIZE


@dataclass(frozen=True)
class ParaStyle:
    """The layout-relevant formatting of a paragraph."""
    size_pt: float = 8.0
    bold: bool = False            # weight of text that carries no explicit emphasis
    line_pct: float = 1.0         # line spacing: 1.0 single, 1.5 = 150 %
    before_pt: float = 0.0
    after_pt: float = 0.0
    indent_pt: float = 0.0        # left margin of the text (a bullet hangs to the left of it)

    def line_height(self) -> float:
        return self.size_pt * LINE_FACTOR * self.line_pct


def spaced(style: ParaStyle, mode: str) -> ParaStyle:
    """A style under one of the builder's spacing modes:
    ``template`` (as authored), ``single`` (single line spacing), ``tight`` (single line spacing and
    at most 3 pt between paragraphs)."""
    if mode == "template":
        return style
    out = replace(style, line_pct=1.0)
    if mode == "tight":
        out = replace(out, before_pt=min(out.before_pt, 3.0), after_pt=min(out.after_pt, 3.0))
    return out


def line_count(segments: list[tuple[str, bool]], size_pt: float, width_pt: float) -> int:
    """Lines needed for a paragraph made of ``(text, bold)`` segments in a `width_pt` wide column."""
    if width_pt <= 0:
        return 1
    words: list[tuple[str, bool]] = []
    for text, bold in segments:
        for i, word in enumerate(text.split(" ")):
            if i:
                words.append((" ", bold))
            if word:
                words.append((word, bold))
    lines, used = 1, 0.0
    pending_space = 0.0
    for word, bold in words:
        w = text_width(word, size_pt, bold) * SAFETY
        if word == " ":
            pending_space += w
            continue
        if used and used + pending_space + w > width_pt:
            lines += 1
            used, pending_space = 0.0, 0.0
        used += pending_space + w
        pending_space = 0.0
        while used > width_pt:        # one unbreakable word wider than the column
            lines += 1
            used -= width_pt
    return lines


def para_height(segments: list[tuple[str, bool]], style: ParaStyle, width_pt: float) -> float:
    """Height of one paragraph including the space before and after it, in points."""
    lines = line_count(segments, style.size_pt, width_pt - style.indent_pt)
    return style.before_pt + lines * style.line_height() + style.after_pt


def fit_text(text: str, style: ParaStyle, width_pt: float, max_lines: int, *,
             whole_sentences: bool = True) -> str:
    """The longest prefix of `text` that fits in `max_lines`.

    With `whole_sentences` (prose) the cut falls between sentences; a title, or prose whose very first
    sentence is already too long, is cut at a word boundary and ends with an ellipsis."""
    import re

    def lines(candidate: str) -> int:
        return line_count([(candidate, style.bold)], style.size_pt, width_pt - style.indent_pt)

    text = " ".join((text or "").split())
    if not text or lines(text) <= max_lines:
        return text
    if whole_sentences:
        kept = ""
        for sentence in re.findall(r".+?(?:[.!?](?=\s|$)|$)", text):
            candidate = f"{kept} {sentence.strip()}".strip()
            if lines(candidate) > max_lines:
                break
            kept = candidate
        if kept:
            return kept
    words = text.split(" ")
    while len(words) > 1:
        words.pop()
        candidate = " ".join(words).rstrip(",;:.") + "\u2026"
        if lines(candidate) <= max_lines:
            return candidate
    return words[0]
