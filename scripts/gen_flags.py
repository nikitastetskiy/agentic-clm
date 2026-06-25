#!/usr/bin/env python3
"""Generate the small country-flag PNGs used for the per-webinar-slot language flag (the circular
flag icon at each slot's top-right on slides 10/14). The editor (edit_event_slides.py) repoints a
slot's flag to assets/flags/<lang>.png when the webinar it fills is detected to be in that language.

The simple tri-band flags are rendered deterministically here (no external downloads). The two flags
the source deck already ships (Union Jack = en, Germany = de) are kept as-is under assets/flags and
are NOT regenerated. Run: python scripts/gen_flags.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image

OUT = Path(__file__).resolve().parent.parent / "assets" / "flags"
W, H = 360, 240  # 3:2; the deck clips each flag to a circle, so exact ratio is not critical


def vertical(bands: list[str]) -> Image.Image:
    img = Image.new("RGB", (W, H))
    n = len(bands)
    for i, hexc in enumerate(bands):
        x0 = i * W // n
        x1 = (i + 1) * W // n
        for x in range(x0, x1):
            for y in range(H):
                img.putpixel((x, y), _rgb(hexc))
    return img


def horizontal(stops: list[tuple[str, float]]) -> Image.Image:
    """stops: list of (hex, fraction-of-height). Fractions must sum to 1."""
    img = Image.new("RGB", (W, H))
    y = 0
    for hexc, frac in stops:
        y1 = min(H, y + round(frac * H))
        for yy in range(y, y1):
            for x in range(W):
                img.putpixel((x, yy), _rgb(hexc))
        y = y1
    return img


def _rgb(hexc: str) -> tuple[int, int, int]:
    hexc = hexc.lstrip("#")
    return (int(hexc[0:2], 16), int(hexc[2:4], 16), int(hexc[4:6], 16))


def usa() -> Image.Image:
    """A simplified Stars and Stripes (13 stripes + a blue canton with star dots). Recognisable when
    the deck clips it to a circle; we don't need all 50 stars at icon size."""
    from PIL import ImageDraw
    red, white, blue = "#B22234", "#FFFFFF", "#3C3B6E"
    img = Image.new("RGB", (W, H), white)
    d = ImageDraw.Draw(img)
    for i in range(13):
        if i % 2 == 0:
            d.rectangle([0, round(i * H / 13), W, round((i + 1) * H / 13)], fill=red)
    cw, ch = round(W * 0.42), round(7 * H / 13)
    d.rectangle([0, 0, cw, ch], fill=blue)
    for r in range(5):
        for c in range(6):
            cx, cy = (c + 0.5) * cw / 6, (r + 0.5) * ch / 5
            d.ellipse([cx - 3, cy - 3, cx + 3, cy + 3], fill=white)
    return img


FLAGS = {
    "it": lambda: vertical(["#009246", "#FFFFFF", "#CE2B37"]),
    "fr": lambda: vertical(["#0055A4", "#FFFFFF", "#EF4135"]),
    "nl": lambda: horizontal([("#AE1C28", 1 / 3), ("#FFFFFF", 1 / 3), ("#21468B", 1 / 3)]),
    "es": lambda: horizontal([("#AA151B", 0.25), ("#F1BF00", 0.5), ("#AA151B", 0.25)]),
    "us": usa,
}


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    for lang, build in FLAGS.items():
        path = OUT / f"{lang}.png"
        build().save(path)
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
