#!/usr/bin/env python3
"""Deterministically fetch recent Terraform Enterprise releases and apply the freshness guard.

Terraform Enterprise is NOT on the public releases API, so this was previously the slowest, most
error-prone part of the deck-data AI phase (the agent would web_fetch the docs page and sometimes
dig through git history for dates — exactly the anti-pattern its prompt warns against). This script
replaces that with a single deterministic HTTP GET + parse:

  * The releases page (developer.hashicorp.com/terraform/enterprise/releases) is a Next.js page; its
    release table is embedded in the ``__NEXT_DATA__`` script as compiled MDX. Each release row links
    to ``/terraform/enterprise/releases/<YEAR>/v<VERSION>`` — a robust, structured anchor that gives
    us both the version (e.g. ``v202507-1`` or a future semver) and its calendar year.
  * For calendar versions (``vYYYYMM-N``) the month/year come straight from the version string.

Freshness guard (never downgrade the deck): TFE has shipped under two schemes (older calendar
``vYYYYMM-N`` and newer semver ``v2.x``). We compare the newest fetched release's DATE against the
newest TFE entry already on slide 8 of the *original* deck. If the live page is OLDER than the deck
(e.g. 2025 calendar versions vs the deck's 2026 semver), it is stale: we write ``recent: []`` /
``latest: null`` / ``status: "stale_source"`` so the build leaves slide 8 unchanged.

Augments data/releases.json -> ``terraform_enterprise`` in place. Run AFTER fetch_releases.py.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.common import DATA_DIR, REPO_ROOT, http_get, load_config, log  # noqa: E402

RELEASES_JSON = DATA_DIR / "releases.json"
MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]

# Release rows link to /terraform/enterprise/releases/<year>/v<version>. Capture year + version.
RELEASE_HREF_RE = re.compile(r"/terraform/enterprise/releases/(\d{4})/(v[\w.\-]+)")
CALENDAR_RE = re.compile(r"^v(\d{4})(\d{2})-(\d+)$")           # v202507-1
SEMVER_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")              # v2.0.3
NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S)


def parse_release_rows(html: str) -> list[dict]:
    """Return [{version, date, month, month_label, scheme, release_notes_url}], newest first."""
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


def deck_latest_tfe_date(original_pptx: Path) -> str | None:
    """Max release date among slide-8's ``Terraform Enterprise vX (YYYY-MM-DD)`` entries."""
    if not original_pptx or not original_pptx.exists():
        return None
    try:
        import shutil
        import subprocess
        import tempfile
        unpack = REPO_ROOT / ".github/skills/pptx/scripts/office/unpack.py"
        tmp = Path(tempfile.mkdtemp(prefix="tfe-deck-"))
        try:
            subprocess.run([sys.executable, str(unpack), str(original_pptx), str(tmp)],
                           check=True, capture_output=True)
            slide8 = tmp / "ppt" / "slides" / "slide8.xml"
            if not slide8.exists():
                return None
            text = slide8.read_text(encoding="utf-8")
            dates = re.findall(r"Terraform Enterprise v[\w.\-]+ \((\d{4}-\d{2}-\d{2})\)", text)
            return max(dates) if dates else None
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001 — guard is best-effort; absence just disables it
        log(f"  could not read deck TFE date ({exc}); freshness guard relaxed")
        return None


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

    rows = parse_release_rows(html)
    log(f"  parsed {len(rows)} releases: {[r['version'] for r in rows[:6]]}")
    if not rows:
        te["status"] = "unavailable"
        te.setdefault("recent", [])
        te["latest"] = None
        _write(data)
        return 0

    # Freshness guard: never downgrade slide 8 to an older source scheme/date.
    deck_date = deck_latest_tfe_date(_original_pptx())
    fetched_latest = rows[0]
    if deck_date and fetched_latest["date"] <= deck_date:
        log(f"  STALE: newest fetched {fetched_latest['version']} ({fetched_latest['date']}) "
            f"<= deck's slide-8 latest ({deck_date}); leaving slide 8 unchanged")
        te["recent"] = []
        te["latest"] = None
        te["status"] = "stale_source"
        te["note"] = (f"Live releases page is older than the deck ({fetched_latest['date']} <= "
                      f"{deck_date}); slide 8 left unchanged (never downgrade).")
        _write(data)
        return 0

    recent = rows[:recent_count]
    te["recent"] = recent
    te["latest"] = recent[0]
    te["status"] = "ok"
    te["note"] = "Fetched deterministically from the releases page (__NEXT_DATA__)."
    log(f"  TFE latest: v{recent[0]['version']} ({recent[0]['date']})")
    _write(data)
    return 0


def _original_pptx() -> Path | None:
    """The read-only source deck at the repo root (first *.pptx that isn't an output copy)."""
    for p in sorted(REPO_ROOT.glob("*.pptx")):
        return p
    return None


def _write(data: dict) -> None:
    RELEASES_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    log(f"updated {RELEASES_JSON.relative_to(REPO_ROOT)} -> terraform_enterprise")


if __name__ == "__main__":
    raise SystemExit(main())
