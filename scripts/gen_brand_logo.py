#!/usr/bin/env python3
"""Generate the featured-event brand logo overlay: the "HashiCorp / An IBM Company" lockup with its
LETTERS filled by the HashiCorp brand gradient (the same blue->purple->pink->orange used on the deck's
"Featured / In Person Events" divider) plus a soft shadow for legibility, on a transparent background.

The deterministic event editor (edit_event_slides.py) places this overlay in the bottom-left corner of
each featured hero photo. This is a maintenance tool (like gen_flags.py): run it only to (re)generate
assets/brand/hashicorp-ibm-gradient.png after changing the source logo, gradient, or tagline.

    python scripts/gen_brand_logo.py

Source logo: assets/brand/_src/hashicorp-logo-pd.png (public-domain HashiCorp wordmark, Wikimedia
Commons — see assets/brand/CREDITS.md). The "An IBM Company" tagline is rendered with a system sans.
"""
from __future__ import annotations

import os
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "assets" / "brand" / "_src" / "hashicorp-logo-pd.png"
OUT = REPO / "assets" / "brand" / "hashicorp-ibm-gradient.png"

# HashiCorp brand gradient (slide 17 "Featured / In Person Events" title), left -> right.
STOPS = [(0.0, (0x6C, 0x81, 0xFF)), (0.34, (0xC0, 0x8D, 0xFF)),
         (0.67, (0xFF, 0x87, 0x91)), (1.0, (0xF9, 0xB5, 0x70))]
WORDMARK_W = 1100          # px width of the HashiCorp wordmark in the lockup
TAGLINE = "An IBM Company"

# Glow behind the lockup for legibility over photos — a soft cool-grey ("plata"/silver-grey) halo
# rather than a heavy black shadow. (R, G, B) and peak alpha (grey needs a touch more alpha than black
# to read on busy/bright photos).
GLOW_COLOR = (120, 123, 132)
GLOW_ALPHA = 0.85


def _font(size: int) -> ImageFont.FreeTypeFont:
    for p in ("/System/Library/Fonts/Supplemental/Arial Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "/System/Library/Fonts/Helvetica.ttc"):
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, size)
            except OSError:
                continue
    return ImageFont.load_default()


def _gradient(w: int, h: int) -> Image.Image:
    g = Image.new("RGB", (w, h))
    px = g.load()
    for x in range(w):
        t = x / max(1, w - 1)
        col = STOPS[-1][1]
        for i in range(len(STOPS) - 1):
            t0, c0 = STOPS[i]
            t1, c1 = STOPS[i + 1]
            if t0 <= t <= t1:
                f = (t - t0) / (t1 - t0)
                col = tuple(int(c0[k] + (c1[k] - c0[k]) * f) for k in range(3))
                break
        for y in range(h):
            px[x, y] = col
    return g


def build() -> Image.Image:
    wm = Image.open(SRC).convert("RGBA")
    bbox = wm.split()[3].getbbox()
    wm = wm.crop(bbox)
    wm = wm.resize((WORDMARK_W, round(wm.height * WORDMARK_W / wm.width)), Image.LANCZOS)

    tag_font = _font(round(WORDMARK_W * 0.092))
    gap = round(WORDMARK_W * 0.03)
    tmp = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    asc, desc = tag_font.getmetrics()
    tag_h = asc + desc

    # Lockup alpha mask: wordmark on top, tagline left-aligned beneath.
    lock = Image.new("RGBA", (WORDMARK_W, wm.height + gap + tag_h), (0, 0, 0, 0))
    lock.alpha_composite(wm, (0, 0))
    ImageDraw.Draw(lock).text((round(WORDMARK_W * 0.006), wm.height + gap), TAGLINE,
                              font=tag_font, fill=(255, 255, 255, 255))
    lock = lock.crop(lock.split()[3].getbbox())
    mask = lock.split()[3]

    W, H = lock.size
    glyph = _gradient(W, H).convert("RGBA")
    glyph.putalpha(mask)

    # Soft GREY/SILVER GLOW for legibility over any photo — a centered, blurred cool-grey halo behind
    # the letters (no visible box, no sticker-like outline). Stacked passes give it presence on busy
    # photos without the heaviness of a black shadow.
    pad = round(H * 0.35)
    canvas = Image.new("RGBA", (W + 2 * pad, H + 2 * pad), (0, 0, 0, 0))
    halo = Image.new("RGBA", canvas.size, GLOW_COLOR + (0,))
    halo_alpha = Image.new("L", canvas.size, 0)
    halo_alpha.paste(mask.point(lambda v: int(v * GLOW_ALPHA)), (pad, pad))
    halo.putalpha(halo_alpha)
    wide = halo.filter(ImageFilter.GaussianBlur(round(H * 0.14)))
    tight = halo.filter(ImageFilter.GaussianBlur(round(H * 0.06)))
    for layer in (wide, wide, tight):
        canvas.alpha_composite(layer)
    canvas.alpha_composite(glyph, (pad, pad))
    return canvas


def main() -> int:
    if not SRC.exists():
        raise SystemExit(f"source logo not found: {SRC}")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    img = build()
    img.save(OUT, format="PNG")
    print(f"wrote {OUT.relative_to(REPO)}  ({img.size[0]}x{img.size[1]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
