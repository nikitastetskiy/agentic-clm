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

import re
from dataclasses import dataclass, replace
from functools import lru_cache

from lib.common import REPO_ROOT

FONT_DIR = REPO_ROOT / "assets" / "fonts"
FACES = {False: "Inter-Regular.ttf", True: "Inter-Bold.ttf"}
REF_SIZE = 1000         # measure at 1000 units and scale: widths are linear in font size
LINE_FACTOR = 1.22      # height of one single-spaced line of Inter, per point of font size
SAFETY = 1.01           # widths are inflated by this much before wrapping
FLAG_RE = re.compile(r"[(\"']?--?[A-Za-z]")   # a command-line flag: -minimal-refresh, --force
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


def _words(segments: list[tuple[str, bool]]) -> list[tuple[str, bool]]:
    """``(text, bold)`` segments as words, each space between them a word of its own."""
    words: list[tuple[str, bool]] = []
    for text, bold in segments:
        for i, word in enumerate(text.split(" ")):
            if i:
                words.append((" ", bold))
            if word:
                words.append((word, bold))
    return words


def line_count(segments: list[tuple[str, bool]], size_pt: float, width_pt: float) -> int:
    """Lines needed for a paragraph made of ``(text, bold)`` segments in a `width_pt` wide column."""
    return _wrapped(_words(segments), size_pt, width_pt)[0]


def cut_points(segments: list[tuple[str, bool]], size_pt: float, width_pt: float,
               heading: bool = False) -> list[int]:
    """Offsets, in the paragraph's text, of the words that need a line break before them to stay whole.

    The wrap below breaks only at spaces, but PowerPoint also breaks after a hyphen: where a date such
    as "(2026-09-16)" does not fit at the end of a line it fills the line with "(2026-09-" and sets
    "16)" on the next, and of a flag such as "-minimal-refresh" it leaves a lone "-" behind. A break
    before the word costs no line, since the word opens the next line either way. Kept whole are the
    date that closes a heading and, in any other text, a command-line flag; an ordinary hyphenated word
    ("multi-architecture", a CVE id) may wrap at its hyphen."""
    words = _words(segments)
    last = max((i for i, (word, _bold) in enumerate(words) if word != " "), default=-1)
    starts = [0]
    for word, _bold in words:
        starts.append(starts[-1] + len(word))

    def whole(i: int) -> bool:
        word = words[i][0]
        return (i == last and "-" in word) if heading else bool(FLAG_RE.match(word))

    return [starts[i] for i in _wrapped(words, size_pt, width_pt)[1] if whole(i)]


def _wrapped(words: list[tuple[str, bool]], size_pt: float, width_pt: float) -> tuple[int, list[int]]:
    """(lines, the indices of the words that open a line after the first)."""
    if width_pt <= 0:
        return 1, []
    lines, used = 1, 0.0
    pending_space = 0.0
    openers = []
    for i, (word, bold) in enumerate(words):
        w = text_width(word, size_pt, bold) * SAFETY
        if word == " ":
            pending_space += w
            continue
        if used and used + pending_space + w > width_pt:
            lines += 1
            openers.append(i)
            used, pending_space = 0.0, 0.0
        used += pending_space + w
        pending_space = 0.0
        while used > width_pt:        # one unbreakable word wider than the column
            lines += 1
            used -= width_pt
    return lines, openers


def para_height(segments: list[tuple[str, bool]], style: ParaStyle, width_pt: float) -> float:
    """Height of one paragraph including the space before and after it, in points."""
    lines = line_count(segments, style.size_pt, width_pt - style.indent_pt)
    return style.before_pt + lines * style.line_height() + style.after_pt


def _lines(text: str, style: ParaStyle, width_pt: float) -> int:
    return line_count([(text, style.bold)], style.size_pt, width_pt - style.indent_pt)


def fit_sentences(text: str, style: ParaStyle, width_pt: float, max_lines: int) -> str:
    """The leading whole sentences of `text` that fit in `max_lines`: all of it when it fits, "" when
    not even its first sentence does."""
    kept = ""
    for sentence in re.findall(r".+?(?:[.!?](?=\s|$)|$)", " ".join((text or "").split())):
        candidate = f"{kept} {sentence.strip()}".strip()
        if _lines(candidate, style, width_pt) > max_lines:
            break
        kept = candidate
    return kept
