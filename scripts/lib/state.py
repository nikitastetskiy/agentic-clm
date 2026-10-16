"""Small, git-tracked build state under state/ — the only things a weekly build cannot re-derive from
its sources on the day it runs.

  * state/blogs.json       every blog post seen so far, per product. A blog slide shows the 8 newest
                           posts, but the sources only reach back ~6 posts per product (listing page)
                           and 20 posts overall (feed); older posts come from this history. It is
                           seeded from the reference deck when the template is cut.
  * state/enrichment.json  wording written once per item (by the enrichment step or by hand) and
                           reused on every later build, so a deck does not get reworded each week.

Files are plain JSON with sorted keys so diffs stay readable. The weekly pipeline commits changes
back; a build that cannot (read-only checkout) still works from what is already in the tree.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from lib.common import REPO_ROOT

# AUTO_PPTX_STATE_DIR redirects the state (the tests run against a copy so they never touch it).
STATE_DIR = Path(os.environ.get("AUTO_PPTX_STATE_DIR") or REPO_ROOT / "state").resolve()
BLOG_HISTORY_CAP = 40  # per product; a slide shows 8


def load(name: str) -> dict:
    path = STATE_DIR / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save(name: str, payload: dict) -> bool:
    """Write state/<name> if its content changed. Returns True when the file was (re)written."""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    path = STATE_DIR / name
    text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True) + "\n"
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.write_text(text, encoding="utf-8")
    return True


def norm_url(url: str) -> str:
    """Identity of a hashicorp.com URL across its spellings (scheme, www, /en/ locale, trailing /)."""
    u = (url or "").strip().lower().split("://", 1)[-1]
    if u.startswith("www."):
        u = u[4:]
    return u.replace("/en/", "/").split("#", 1)[0].split("?", 1)[0].rstrip("/")


def event_key(url: str) -> str:
    """Identity of an event's link. Unlike a blog URL the query string is part of it (webcast links
    differ only there); the scheme, "www." and the /en/ locale segment are not."""
    u = (url or "").strip().split("://", 1)[-1]
    host, _, rest = u.partition("/")
    host = host.lower()
    if host.startswith("www."):
        host = host[4:]
    rest = rest[3:] if rest.startswith("en/") else rest
    return f"{host}/{rest}".rstrip("/")


def merge_blog_posts(known: list[dict], fresh: list[dict]) -> list[dict]:
    """Union of two post lists keyed by URL, newest first.

    A post keeps the date it was first recorded with. The feed only carries a last-UPDATED timestamp,
    so letting it overwrite a known date would move an edited post back to the top of the slide; only
    a listing page (``source: "product-page"``), which shows the publish date, may correct a date."""
    by_url: dict[str, dict] = {}
    for post in known:
        if post.get("url") and post.get("date") and post.get("title"):
            by_url[norm_url(post["url"])] = {"date": post["date"], "title": post["title"],
                                             "url": post["url"]}
    for post in fresh:
        if not (post.get("url") and post.get("date") and post.get("title")):
            continue
        key = norm_url(post["url"])
        date = post["date"]
        if key in by_url and post.get("source") != "product-page":
            date = by_url[key]["date"]
        # The same post recorded earlier under a different (mistyped) link: the fresh copy wins.
        title = norm_title(post["title"])
        for other in [k for k, v in by_url.items() if k != key and norm_title(v["title"]) == title]:
            del by_url[other]
        by_url[key] = {"date": date, "title": post["title"], "url": post["url"]}
    # Stable sort by date only: among posts of the same day, one already recorded (and so already
    # on the slide) is not displaced by a newcomer.
    return sorted(by_url.values(), key=lambda p: p["date"], reverse=True)[:BLOG_HISTORY_CAP]


def norm_title(title: str) -> str:
    return " ".join("".join(c if c.isalnum() else " " for c in title.lower()).split())
