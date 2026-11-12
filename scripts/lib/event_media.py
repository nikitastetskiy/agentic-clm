"""The pictures on the event slides, and the flag each card's language calls for.

A webinar card carries a round language flag and its products' icons; a featured-event slide carries one
hero photo (the event's city, cropped to cover its frame, corners rounded) with the brand logo over its
corner. The builder swaps these per event, always as fresh media behind a fresh relationship: a picture
relationship is often shared between slides, and repointing one in place would change another slide.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

from lib import pptx_edit as px
from lib import slides as sl
from lib.common import REPO_ROOT

# Rounded-corner radius (a:prstGeom roundRect adj guide) of a featured event's hero photo: every swapped
# hero gets it, whatever shape the template's placeholder had, so all event photos look alike.
HERO_CORNER_ADJ = 1178

# The "HashiCorp / An IBM Company" lockup laid over the hero's bottom-left corner (assets/brand/, drawn
# by scripts/gen_brand_logo.py). WIDTH_FRAC is its width as a fraction of the hero's; INSET_FRAC its
# padding from the corner.
BRAND_LOGO = REPO_ROOT / "assets" / "brand" / "hashicorp-ibm-gradient.png"
BRAND_LOGO_WIDTH_FRAC = 0.46
BRAND_LOGO_INSET_FRAC = 0.012

# The round language flag at a card's top-right. The reference deck ships the Union Jack (en) and the
# German flag (de); the others are drawn by scripts/gen_flags.py.
FLAG_DIR = REPO_ROOT / "assets" / "flags"
# Product logos, each a byte-identical copy of the deck's own media, so a content hash tells which
# product a small icon stands for (match_product_icons). File stem == the product key in events.json.
PRODUCT_ICON_DIR = REPO_ROOT / "assets" / "product-icons"
FLAG_DESCR = {
    "en": "Union Jack - Wikipedia",
    "us": "Flag of the United States - Wikipedia",
    "de": "Flag of Germany - Wikipedia",
    "it": "Flag of Italy - Wikipedia",
    "fr": "Flag of France - Wikipedia",
    "es": "Flag of Spain - Wikipedia",
    "nl": "Flag of the Netherlands - Wikipedia",
}
# Map an existing flag's alt-text (descr) back to a language/region code so an already-correct flag is
# left alone. US -> "us", UK -> "en": both are ENGLISH, but we keep them distinct so an English webinar
# never flips the deck author's US-vs-UK choice (we can't tell US from UK English by the title).
_DESCR_LANG = [("union jack", "en"), ("united kingdom", "en"), ("great britain", "en"),
               ("united states", "us"), ("u.s.", "us"), ("usa", "us"), ("american", "us"),
               ("stars and stripes", "us"), ("germany", "de"), ("italy", "it"),
               ("france", "fr"), ("spain", "es"), ("netherlands", "nl")]
# Distinctive function/content words per language (lowercase). Title-level language detection — short
# but enough to tell an English webinar from an Italian/French/Spanish/German/Dutch regional one.
_LANG_WORDS = {
    "it": {"identità", "accesso", "controllo", "proteggere", "sicurezza", "gestione", "della",
           "degli", "alla", "gli", "nel", "una", "con", "dei", "sulla", "distribuire"},
    "fr": {"sécurité", "gestion", "accès", "comment", "déployer", "gérer", "avec", "pour", "votre",
           "vos", "les", "des", "une", "dans", "sur", "afin", "déploiement"},
    "es": {"seguridad", "gestión", "acceso", "cómo", "para", "con", "una", "los", "las", "del",
           "su", "sus", "implementar", "nube"},
    "de": {"sicherheit", "verwaltung", "zugriff", "mit", "für", "und", "der", "die", "das", "ihre",
           "von", "auf", "den", "wie", "bereitstellen"},
    "nl": {"beveiliging", "beheer", "toegang", "met", "voor", "een", "het", "van", "uw", "hoe",
           "naar", "implementeren"},
}


def detect_language(title: str) -> str:
    """Best-effort language of a webinar title -> ISO code in FLAG_DESCR (default 'en'). Word-based
    with a small boost from unambiguous diacritics; tuned to separate English from the EMEA regional
    languages that occasionally appear in the webinar feed."""
    low = (title or "").lower()
    if not low.strip():
        return "en"
    toks = re.findall(r"[a-zàáâäãèéêëìíîïòóôöõùúûüçñ]+", low)
    scores = {lang: 0 for lang in _LANG_WORDS}
    for t in toks:
        for lang, words in _LANG_WORDS.items():
            if t in words:
                scores[lang] += 1
    if any(c in low for c in "ìòù"):
        scores["it"] += 1
    if any(c in low for c in "äöüß"):
        scores["de"] += 1
    if any(c in low for c in "ñ¿¡"):
        scores["es"] += 1
    best = max(scores, key=lambda k: scores[k])
    return best if scores[best] > 0 else "en"


def _image_size(path: Path) -> tuple[int, int]:
    from PIL import Image  # lazy: Pillow is only needed when a picture is placed
    with Image.open(path) as image:
        return image.size


# --------------------------------------------------------------------------- language flags -------
def _descr_lang(descr: str) -> str | None:
    low = (descr or "").lower()
    return next((code for word, code in _DESCR_LANG if word in low), None)


def set_slot_flag(pic, rels_dom, root: Path, slide_n: int, idx: int, lang: str) -> bool:
    """Show the flag of `lang` on a card, as fresh media; False when the card's flag is already right.
    An English session keeps whichever English flag the card has, UK or US: a title cannot tell them
    apart, and the choice was the deck author's."""
    lang = lang if lang in FLAG_DESCR else "en"
    cnv = pic.getElementsByTagName("p:cNvPr")[0]
    current = _descr_lang(cnv.getAttribute("descr"))
    asset = FLAG_DIR / f"{lang}.png"
    if current == lang or (lang == "en" and current == "us") or not asset.exists():
        return False
    rid = px.embed_media(root, rels_dom, asset, f"image_flag_s{slide_n}_{idx}.png")
    pic.getElementsByTagName("a:blip")[0].setAttribute("r:embed", rid)
    cnv.setAttribute("descr", FLAG_DESCR[lang])
    return True


# --------------------------------------------------------------------------- the event photo ------
def _pictures(dom) -> list[tuple[int, str, object]]:
    """(area, name, picture) for every picture of the slide, largest first."""
    sized = [(sl.box(pic), pic) for pic in dom.getElementsByTagName("p:pic")]
    return sorted(((box[2] * box[3], px.shape_name(pic), pic) for box, pic in sized if box),
                  key=lambda t: t[0], reverse=True)


def find_hero_pic(dom):
    """The slide's event photo, or None. The largest picture is the full-bleed background; of the
    others, the template's 'Picture Placeholder' if there is one, else the largest that is not a
    small logo (more than 1 % of the background)."""
    pics = _pictures(dom)
    if len(pics) < 2:
        return None
    background, rest = pics[0][0], pics[1:]
    candidates = ([t for t in rest if "placeholder" in t[1].lower()]
                  or [t for t in rest if t[0] > 0.01 * background])
    return max(candidates, key=lambda t: t[0])[2] if candidates else None


def remove_nonhero_pics(dom, hero) -> None:
    """One event photo per slide: besides `hero`, the full-bleed background and the small corner logos
    stay; every other picture (a screenshot of another event's page, a second skyline) goes."""
    pics = _pictures(dom)
    for area, _name, pic in pics:
        if pic is not hero and 0.012 * pics[0][0] <= area < pics[0][0]:
            px.remove_node(pic)


def _cover_srcrect(frame_cx: int, frame_cy: int, img_w: int, img_h: int) -> dict:
    """The source crop (a:srcRect, in 1/1000 of a percent) with which an image fills a frame without
    distortion: top and bottom are cut when the image is taller than the frame, left and right when it
    is wider. {} when the proportions already match."""
    frame, image = frame_cx / frame_cy, img_w / img_h
    if abs(frame - image) < 1e-4:
        return {}
    if frame > image:
        cut = round((1 - image / frame) / 2 * 100000)
        return {"t": cut, "b": cut}
    cut = round((1 - frame / image) / 2 * 100000)
    return {"l": cut, "r": cut}


def swap_event_image(hero, rels_dom, root: Path, slide_n: int, photo: Path) -> None:
    """Show `photo` in the hero picture's frame: as fresh media, cropped to cover the frame (the crop
    the template carries was made for another photo), corners rounded like every event photo's."""
    blip = hero.getElementsByTagName("a:blip")[0]
    name = f"image_evt_slide{slide_n}{photo.suffix.lower() or '.png'}"
    blip.setAttribute("r:embed", px.embed_media(root, rels_dom, photo, name))
    for old in hero.getElementsByTagName("a:srcRect"):
        old.parentNode.removeChild(old)
    _x, _y, cx, cy = sl.box(hero)
    crop = _cover_srcrect(cx, cy, *_image_size(photo))
    if crop:
        rect = hero.ownerDocument.createElement("a:srcRect")
        for side, value in crop.items():
            rect.setAttribute(side, str(value))
        blip.parentNode.insertBefore(rect, blip.nextSibling)       # order: blip, srcRect, stretch

    geom = hero.getElementsByTagName("a:prstGeom")[0]
    geom.setAttribute("prst", "roundRect")
    for old in list(geom.getElementsByTagName("a:avLst")):
        geom.removeChild(old)
    guide = hero.ownerDocument.createElement("a:gd")
    guide.setAttribute("name", "adj")
    guide.setAttribute("fmla", f"val {HERO_CORNER_ADJ}")
    geom.appendChild(hero.ownerDocument.createElement("a:avLst")).appendChild(guide)


def place_brand_logo(dom, rels_dom, root: Path, hero, slide_n: int) -> None:
    """Lay the HashiCorp / IBM lockup over the hero photo's bottom-left corner: a new picture, last in
    the shape tree so that it is drawn on top."""
    x, y, cx, cy = sl.box(hero)
    logo_w, logo_h = _image_size(BRAND_LOGO)
    width = round(cx * BRAND_LOGO_WIDTH_FRAC)
    height = round(width * logo_h / logo_w)
    inset = round(cx * BRAND_LOGO_INSET_FRAC)
    logo = px.new_picture(dom, rels_dom, root, BRAND_LOGO, f"image_brand_slide{slide_n}{BRAND_LOGO.suffix.lower()}",
                          f"HashiCorp IBM Logo {slide_n}", (x + inset, y + cy - height - inset, width, height))
    sl.sp_tree(dom).appendChild(logo)


# --------------------------------------------------------------------------- product icons --------
def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _media_of(pic, rels_dom, root: Path) -> list[Path]:
    """The media files a picture embeds. Reads <a:blip> AND <asvg:svgBlip>: the Terraform icon is an
    SVG that only the svgBlip points at."""
    target = {r.getAttribute("Id"): r.getAttribute("Target") for r in rels_dom.getElementsByTagName("Relationship")}
    rids = [b.getAttribute("r:embed") for tag in ("a:blip", "asvg:svgBlip") for b in pic.getElementsByTagName(tag)]
    files = [(root / "ppt" / "slides" / target[rid]).resolve() for rid in rids if target.get(rid)]
    return [f for f in files if f.is_file()]


def match_product_icons(dom, rels_dom, root: Path, event: dict) -> dict:
    """Show only the logos of the products the event is about: a featured-event slide carries the
    template event's product logos, and a Vault-only session must not keep the Terraform icon. A logo
    is recognised by its content: each file in assets/product-icons/ is a byte-identical copy of the
    deck's own media (a partner logo's colour can resemble a product's, so colour would not do). Every
    other picture — a partner logo, the photo, the brand lockup — is left alone, and so is everything
    when the event names no product. Returns what was kept and removed, for the build report."""
    products = sorted({str(p).strip().lower() for p in event.get("products") or [] if str(p).strip()})
    result = {"kept": [], "removed": [], "products": products}
    logos = {_digest(f): f.stem.lower() for f in sorted(PRODUCT_ICON_DIR.glob("*"))
             if f.is_file() and f.suffix.lower() != ".md" and not f.name.startswith(".")} if products else {}
    for pic in list(dom.getElementsByTagName("p:pic")):
        product = next((logos[d] for d in map(_digest, _media_of(pic, rels_dom, root)) if d in logos), None)
        if product in products:
            result["kept"].append(product)
        elif product:
            px.remove_node(pic)
            result["removed"].append(product)
    return result
