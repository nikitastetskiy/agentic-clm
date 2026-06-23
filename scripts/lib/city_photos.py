"""Shared resolver for featured-event hero **city photos**.

The featured-event slides (18-22) use the event city's photo as the hero image — NOT a webpage
leadspace/series screenshot (those duplicated the slide's own title/date/description text, carried a
stray divider line and cropped badly). This module maps an event location (events.json "location")
to a normalized PNG that is ready to embed, so the editor (`edit_event_slides.py`) and the verifier
(`verify_deck.py`) resolve the *same* photo and stay in lockstep.

Sources (config/city-photos.yaml):
  * bundled : a city photo already shipped in the SOURCE deck's media (extracted from the deck zip).
  * file    : a freely-licensed photo committed under assets/city-photos/ (see its CREDITS.md).

Every resolved photo is normalized to a deterministic PNG under data/event-images/ so repeated
resolution (editor then verifier) yields byte-identical files.
"""
from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from pathlib import Path

from lib.common import DATA_DIR, load_config

REPO_ROOT = DATA_DIR.parent
CACHE_DIR = DATA_DIR / "event-images"


def norm_city(s: str) -> str:
    """Normalize a location string for lookup: strip accents, lower-case, drop '.'/',', collapse whitespace."""
    s = (s or "").strip()
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[.,]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _slug(key: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", key).strip("-") or "city"


def _save_png(src, dst: Path) -> None:
    """Write `src` (a path or file-like) to `dst` as a deterministic RGB PNG."""
    from PIL import Image  # lazy: Pillow is only needed when city photos are resolved
    with Image.open(src) as im:
        im.convert("RGB").save(dst, format="PNG", optimize=False)


def build_city_photo_cache(original_deck: Path | str | None) -> dict[str, Path]:
    """Resolve config/city-photos.yaml -> {normalized_city: Path to an embeddable PNG}.

    Bundled photos are extracted from `original_deck`'s media; 'file' photos come from
    assets/city-photos/. A city whose source is missing is simply omitted (its slide then keeps its
    existing template photo). Never raises — returns {} if the config or Pillow is unavailable.
    """
    try:
        cfg = load_config("city-photos.yaml")
    except (OSError, RuntimeError, ValueError):
        return {}
    if not isinstance(cfg, dict):
        return {}
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    zf = None
    if original_deck and Path(original_deck).exists():
        try:
            zf = zipfile.ZipFile(original_deck)
        except (OSError, zipfile.BadZipFile):
            zf = None

    out: dict[str, Path] = {}
    for city, spec in (cfg.get("cities") or {}).items():
        if not isinstance(spec, dict):
            continue
        key = norm_city(city)
        dst = CACHE_DIR / f"city-{_slug(key)}.png"
        try:
            if spec.get("source") == "bundled" and zf is not None:
                data = zf.read(f"ppt/media/{spec.get('media')}")
                _save_png(io.BytesIO(data), dst)
            elif spec.get("source") == "file":
                p = (REPO_ROOT / str(spec.get("file"))).resolve()
                if not p.exists():
                    continue
                _save_png(p, dst)
            else:
                continue
            out[key] = dst
        except (KeyError, OSError, ValueError, ImportError):
            continue

    for alias, target in (cfg.get("aliases") or {}).items():
        ak, tk = norm_city(alias), norm_city(target)
        if ak not in out and tk in out:
            out[ak] = out[tk]
    return out


def resolve_city_photo(location: str, cache: dict[str, Path]) -> Path | None:
    """Look up the embeddable city photo for an event `location`, or None if none is configured."""
    return cache.get(norm_city(location))
