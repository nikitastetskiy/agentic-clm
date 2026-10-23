#!/usr/bin/env python3
"""Deterministically fetch recent Terraform Enterprise releases and apply the freshness guard.

Terraform Enterprise is NOT on the public releases API, so this was previously the slowest, most
error-prone part of the deck-data AI phase (the agent would web_fetch the docs page and sometimes
dig through git history for dates — exactly the anti-pattern its prompt warns against). This script
replaces that with a single deterministic HTTP GET + parse:

  * The releases page (developer.hashicorp.com/terraform/enterprise/releases) is a Next.js page.
    TFE 2.x ships under a SEMVER scheme: each ``N.M`` series has its own page
    (``…/releases/2.0.x``) whose release notes list every version as a heading (``id="2-0-3"``) with
    its date in an adjacent span — rendered as ``2.0.3 2026-06-03``. We pick the newest series links,
    fetch those pages, and read version + REAL date from them (parse_series_page).
  * OLDER calendar releases (``vYYYYMM-N``) live under ``/<year>/v<version>`` anchors; parse_release_rows
    handles them as a FALLBACK for pages that expose no semver series. Month/year come from the version.

Freshness guard (never downgrade the deck): TFE has shipped under two schemes (older calendar
``vYYYYMM-N`` and newer semver ``v2.x``). We compare the newest fetched release's DATE against the
newest TFE entry already on the Terraform Enterprise slide of the source deck — the reference deck in
the repo root, or the weekly template when there is none (root decks are git-ignored). The slide is
found by its role, never by its number. If the live page is OLDER than the deck (e.g. 2025 calendar
versions vs the deck's 2026 semver), it is stale: we write ``recent: []`` / ``latest: null`` /
``status: "stale_source"`` so the in-place updater leaves the TFE slide unchanged.

The weekly deck reads only ``releases``: EVERY release in the history window (with its notes grouped
by section), independent of the freshness guard — a deck built from scratch has no previous slide to
protect.

Augments data/releases.json -> ``terraform_enterprise`` in place. Run AFTER fetch_releases.py.
"""
from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path
from xml.dom import minidom

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import deck, htmldoc  # noqa: E402
from lib import pptx_edit as px  # noqa: E402
from lib.common import DATA_DIR, REPO_ROOT, http_get, load_config, log, window_start  # noqa: E402

RELEASES_JSON = DATA_DIR / "releases.json"
MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]

# OLD calendar scheme: rows link to /terraform/enterprise/releases/<year>/v<version>.
RELEASE_HREF_RE = re.compile(r"/terraform/enterprise/releases/(\d{4})/(v[\w.\-]+)")
CALENDAR_RE = re.compile(r"^v(\d{4})(\d{2})-(\d+)$")           # v202507-1
SEMVER_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")              # v2.0.3
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)

# NEW semver scheme: TFE 2.x ships as a per-series page (…/releases/2.0.x) whose release notes list
# each version as a heading with its date in an adjacent span — e.g. rendered "2.0.3 2026-06-03".
SERIES_LINK_RE = re.compile(r"/terraform/enterprise/releases/(\d+)\.(\d+)\.x")  # 2.0.x, 1.2.x …
VERSION_HEADING_RE = re.compile(r'id="(\d+-\d+-\d+)"')         # <h2 id="2-0-3">
VER_DATE_RE = re.compile(r"(\d+\.\d+\.\d+)\s+(\d{4}-\d{2}-\d{2})")
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"(?is)<(script|style)\b.*?</\1>")
RELEASES_BASE = "https://developer.hashicorp.com/terraform/enterprise/releases"
SLIDE_PART_RE = re.compile(r"ppt/slides/slide\d+\.xml")
DECK_ENTRY_RE = re.compile(r"Terraform Enterprise v[\w.\-]+ \((\d{4}-\d{2}-\d{2})\)")

# One-line highlight: the first sentence of the highest-PRIORITY release-notes section. Caveat
# sections ("Known Issues", "Deprecations") are intentionally excluded as a slide headline.
_HL_PREF = ["Security", "Breaking Changes", "Features", "Enhancements", "Improvements", "Bug Fixes"]
_HL_LABEL = {"Security": "Security", "Breaking Changes": "Breaking change", "Features": "New",
             "Enhancements": "Enhancement", "Improvements": "Improvement", "Bug Fixes": "Bug fix"}


def _strip_tags(html_text: str) -> str:
    import html as _html
    s = _SCRIPT_RE.sub(" ", html_text)  # drop __NEXT_DATA__/style so only rendered prose remains
    return _html.unescape(_TAG_RE.sub(" ", s))


def extract_highlight(section: str) -> str | None:
    """A concise, factual one-liner for a release from its notes section (deck-data may polish it)."""
    for cat in _HL_PREF:
        k = section.find(cat)
        if k < 0:
            continue
        rest = re.sub(r"^\(Updated[^)]*\)\s*", "", section[k + len(cat):].strip())
        rest = re.sub(r"^[\s:\u2013\u2014-]+", "", rest)
        sent = re.sub(r"\s+", " ", re.split(r"(?<=[.!?])\s", rest, maxsplit=1)[0].strip())
        if not sent:
            continue
        words = sent.split()
        if len(words) > 24:
            sent = " ".join(words[:24]).rstrip(".,;:") + "\u2026"
        return f"{_HL_LABEL[cat]}: {sent}"
    return None


def semver_series(main_html: str) -> list[tuple[int, int]]:
    """All ``N.M`` semver series linked on the page, newest first (e.g. [(2,0),(1,2),(1,1),(1,0)])."""
    pairs = {(int(a), int(b)) for a, b in SERIES_LINK_RE.findall(main_html)}
    return sorted(pairs, reverse=True)


def parse_series_page(html_text: str, series: str) -> list[dict]:
    """Parse a TFE semver series page (…/releases/<series>.x) -> rows newest-first with REAL dates.

    Only versions that appear BOTH as a release heading (``id="2-0-3"``) AND immediately ahead of an
    ISO date in the rendered text are kept, so the component-versions table (versions with no adjacent
    date) and stray numbers can't be mistaken for releases. Each row also gets a one-line highlight
    extracted from its notes section.
    """
    headings = {h.replace("-", ".") for h in VERSION_HEADING_RE.findall(html_text)}
    text = _strip_tags(html_text)
    marks = list(VER_DATE_RE.finditer(text))
    rows: list[dict] = []
    seen: set[str] = set()
    for i, m in enumerate(marks):
        ver, date = m.group(1), m.group(2)
        if ver in seen or ver not in headings:
            continue
        seen.add(ver)
        end = marks[i + 1].start() if i + 1 < len(marks) else m.start() + 3000
        yyyy, mm, _dd = date.split("-")
        try:
            label = MONTHS[int(mm) - 1]
        except (ValueError, IndexError):
            label = None
        rows.append({
            "version": ver,
            "date": date,
            "month": f"{yyyy}-{mm}",
            "month_label": label,
            "scheme": "semver",
            "highlight": extract_highlight(text[m.start():end]),
            "release_notes_url": f"{RELEASES_BASE}/{series}.x#{ver.replace('.', '-')}",
        })
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
SECTION_ITEMS = 8        # notes kept per section in ``sections``
SECTION_ITEM_CHARS = 420


def parse_series_sections(html_text: str) -> dict[str, dict[str, list[dict]]]:
    """version -> its release notes grouped by section ("Features", "Bug Fixes", "Security", …), read
    from the page structure: each release is a heading whose id is its version (``2-0-8``), followed
    by one sub-heading + list per section. Complements parse_series_page (versions + dates)."""
    doc = htmldoc.parse(html_text)
    version_id = re.compile(r"\d+-\d+-\d+")
    out: dict[str, dict[str, list[dict]]] = {}
    for head in htmldoc.headings_with_id(doc, version_id.pattern):
        body = htmldoc.entry_body(head, lambda h: bool(version_id.fullmatch(h.get("id") or "")))
        groups = htmldoc.sections(body)
        groups.pop("", None)  # preamble: "Last required release", container digests
        out[head.get("id").replace("-", ".")] = {
            name: [{**item, "text": item["text"][:SECTION_ITEM_CHARS]} for item in items[:SECTION_ITEMS]]
            for name, items in groups.items()}
    return out


def parse_release_rows(html: str) -> list[dict]:
    """FALLBACK (older calendar scheme): parse /<year>/v<version> rows from __NEXT_DATA__, newest first.

    Used only when the page exposes no semver series pages (see parse_series_page for the 2.x scheme)."""
    m = NEXT_DATA_RE.search(html)
    blob = m.group(1) if m else html  # fall back to scanning the whole page
    seen: set[str] = set()
    rows: list[dict] = []
    for year, ver in RELEASE_HREF_RE.findall(blob):
        if ver in seen:
            continue
        seen.add(ver)
        cal = CALENDAR_RE.match(ver)
        if cal:
            yyyy, mm = cal.group(1), cal.group(2)
            date = f"{yyyy}-{mm}-01"
            scheme = "calendar"
        elif SEMVER_RE.match(ver):
            date = f"{year}-01-01"  # semver rows carry no month on the page; year is best-effort
            yyyy, mm = year, "01"
            scheme = "semver"
        else:
            continue
        try:
            label = MONTHS[int(mm) - 1]
        except (ValueError, IndexError):
            label = None
        rows.append({
            "version": ver.lstrip("v"),
            "date": date,
            "month": f"{yyyy}-{mm}",
            "month_label": label,
            "scheme": scheme,
            "highlight": None,
            "release_notes_url": f"https://developer.hashicorp.com/terraform/enterprise/releases/{year}/{ver}",
        })
    # The page lists newest first; preserve that order.
    return rows


def tfe_slide_rule() -> dict:
    """The Terraform Enterprise slide's entry in config/deck.yaml: its role, and the `detect` pattern
    that finds it in a reference deck that carries no role stamps."""
    return next(rule for rule in load_config("deck.yaml")["slides"]
                if (rule.get("source") or {}).get("product") == "terraform-enterprise")


def deck_latest_tfe_date(pptx: Path | None) -> str | None:
    """Max release date among the ``Terraform Enterprise vX (YYYY-MM-DD)`` entries of the deck's TFE
    slide: the slide stamped with its role (the template) or, in a reference deck without stamps, the
    slide config/deck.yaml's `detect` matches — never a slide number, which a reordered deck moves."""
    if not pptx or not pptx.exists():
        return None
    try:
        rule = tfe_slide_rule()
        with zipfile.ZipFile(pptx) as zf:
            slides = [minidom.parseString(zf.read(name)) for name in zf.namelist()
                      if SLIDE_PART_RE.fullmatch(name)]
    except Exception as exc:  # noqa: BLE001 — guard is best-effort; absence just disables it
        log(f"  could not read the TFE slide of {pptx.name} ({exc}); freshness guard relaxed")
        return None
    stamp = deck.ROLE_PREFIX + rule["role"]
    detect = re.compile(rule["detect"], re.I)
    stamped = [s for s in slides
               if any(c.getAttribute("name") == stamp for c in s.getElementsByTagName("p:cSld"))]
    titled = [s for s in slides
              if any(detect.search(" ".join(px.shape_text(sp).split())) for sp in s.getElementsByTagName("p:sp"))]
    # Read per paragraph, so an entry whose version is a run of its own (a hyperlink) still matches.
    dates = [d for s in (stamped or titled) for p in s.getElementsByTagName("a:p")
             for d in DECK_ENTRY_RE.findall(px.shape_text(p))]
    return max(dates) if dates else None


def source_deck(root: Path = REPO_ROOT) -> Path:
    """The deck the guard protects: the reference deck in the repo root (what the in-place updater
    edits) or, without one — root decks are git-ignored, so a pipeline checkout has none — the weekly
    template, which carries the TFE entries of the deck it was cut from."""
    for p in sorted(root.glob("*.pptx")):
        return p
    return root / load_config("deck.yaml")["deck"]["template"]


def main() -> int:
    cfg = load_config()
    tfe_cfg = cfg.get("terraform_enterprise", {})
    page = tfe_cfg.get("releases_page")
    recent_count = int(cfg.get("releases", {}).get("recent_count", 4))
    if not page:
        log("no terraform_enterprise.releases_page configured")
        return 1
    if not RELEASES_JSON.exists():
        log("releases.json not found — run fetch_releases.py first")
        return 1

    data = json.loads(RELEASES_JSON.read_text(encoding="utf-8"))
    te = data.setdefault("terraform_enterprise", {})
    te.setdefault("display", tfe_cfg.get("display", "Terraform Enterprise"))
    te["releases_page"] = page
    te["releases"] = []
    since = window_start(int(cfg.get("releases", {}).get("history_months", 6)))

    log(f"fetching TFE releases page: {page}")
    try:
        html = http_get(page).decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001
        log(f"  TFE page fetch failed: {exc}; leaving terraform_enterprise for the agent")
        te["status"] = "unavailable"
        te.setdefault("recent", [])
        te["latest"] = None
        _write(data)
        return 0

    # Prefer the NEW semver series scheme (TFE 2.x): the newest releases live on per-series pages
    # (…/releases/2.0.x) with real dates. Walk series newest-first until we have enough, then fall
    # back to the OLD calendar parser only if the page exposes no semver series at all.
    rows: list[dict] = []
    series = semver_series(html)
    if series:
        log(f"  semver series found: {['%d.%d.x' % s for s in series]}")
        seen_ver: set[str] = set()
        for maj, minr in series[:4]:  # cap requests
            s = f"{maj}.{minr}"
            try:
                shtml = http_get(f"{RELEASES_BASE}/{s}.x").decode("utf-8", "replace")
            except Exception as exc:  # noqa: BLE001
                log(f"    series {s}.x fetch failed: {exc}")
                continue
            new = [r for r in parse_series_page(shtml, s) if r["version"] not in seen_ver]
            notes = parse_series_sections(shtml)
            for r in new:
                r["sections"] = notes.get(r["version"], {})
                r["url"] = r["release_notes_url"]
            seen_ver.update(r["version"] for r in new)
            rows.extend(new)
            log(f"    {s}.x: {[r['version'] for r in new]}")
            # An older series is still maintained alongside the newest one (1.2.x patches ship next
            # to 2.0.x), so keep walking until a series has nothing inside the history window.
            if len(rows) >= recent_count and not any(r["date"] >= since for r in new):
                break
        rows.sort(key=lambda r: r["date"], reverse=True)
    if not rows:
        log("  no semver series rows; falling back to the calendar parser")
        rows = parse_release_rows(html)
    log(f"  parsed {len(rows)} releases: {[r['version'] for r in rows[:6]]}")
    if not rows:
        te["status"] = "unavailable"
        te.setdefault("recent", [])
        te["latest"] = None
        _write(data)
        return 0

    te["releases"] = [r for r in rows if r.get("scheme") == "semver" and r["date"] >= since]
    log(f"  {len(te['releases'])} TFE release(s) in the history window (since {since})")

    # Freshness guard: never downgrade the TFE slide to an older source scheme/date.
    source = source_deck()
    deck_date = deck_latest_tfe_date(source)
    fetched_latest = rows[0]
    if deck_date and fetched_latest["date"] <= deck_date:
        log(f"  STALE: newest fetched {fetched_latest['version']} ({fetched_latest['date']}) "
            f"<= the TFE slide's latest in {source.name} ({deck_date}); leaving the TFE slide unchanged")
        te["recent"] = []
        te["latest"] = None
        te["status"] = "stale_source"
        te["note"] = (f"Live releases page is older than the deck ({fetched_latest['date']} <= "
                      f"{deck_date}); the TFE slide left unchanged (never downgrade).")
        _write(data)
        return 0

    recent = rows[:recent_count]
    te["recent"] = recent
    te["latest"] = recent[0]
    te["status"] = "ok"
    te["note"] = "Fetched deterministically from the releases page (semver series notes)."
    log(f"  TFE latest: v{recent[0]['version']} ({recent[0]['date']})")
    _write(data)
    return 0


def _write(data: dict) -> None:
    RELEASES_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"updated {RELEASES_JSON.relative_to(REPO_ROOT)} -> terraform_enterprise")


if __name__ == "__main__":
    raise SystemExit(main())
