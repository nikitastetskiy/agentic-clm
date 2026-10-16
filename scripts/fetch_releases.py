#!/usr/bin/env python3
"""Fetch HashiCorp release history from the public releases API (Terraform, Vault).

Writes data/releases.json. Per product:

  * ``releases`` — EVERY release in the history window (config ``releases.history_months``), newest
    first, paging through the API (20 per page). This is what the weekly deck is built from: each
    month column lists every release of that month, so the whole window has to be re-derivable from
    the source on every run. Pre-releases are included when the product sets ``deck_prereleases``
    (the Terraform Core slide lists alpha/beta/rc builds next to the stable patches). Vault publishes
    each version as several build variants (+ent, +ent.hsm, …) which are folded into one entry.
  * ``latest`` / ``recent`` / ``month_window`` — the stable-only view the in-place deck updater
    (edit_release_slides.py) reads. Unchanged.

Highlights are added afterwards by fetch_changelogs.py / fetch_release_notes.py. Terraform Enterprise
is not on this API (see fetch_tfe_releases.py).
"""
from __future__ import annotations

import sys
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.common import (http_get_json, load_config, log, month_label, window_start,  # noqa: E402
                        write_output)

PAGE_SIZE = 20   # API maximum
MAX_PAGES = 40   # hard stop; Vault needs ~1 page per release date (4 lines x 5 build variants)


def base_semver(version: str) -> str:
    """Strip build metadata: '2.0.3+ent.hsm.fips1403' -> '2.0.3'."""
    return version.split("+", 1)[0]


def is_stable(version: str, is_prerelease: bool) -> bool:
    if is_prerelease:
        return False
    pre = base_semver(version).lower()
    return not any(tag in pre for tag in ("alpha", "beta", "rc", "dev", "-pre"))


def major_minor(version: str) -> str:
    """'2.0.3' -> '2.0' (the release LINE; used to scope same-line history)."""
    return ".".join(base_semver(version).split(".")[:2])


def fetch_raw(api_base: str, slug: str, since: str) -> list[dict]:
    """Every API record created on/after `since` (ISO date), newest first. The API returns at most 20
    records per call and continues after the ``timestamp_created`` passed as ``after``."""
    out: list[dict] = []
    after = None
    for _ in range(MAX_PAGES):
        url = f"{api_base}/{slug}?limit={PAGE_SIZE}" + (f"&after={quote(after)}" if after else "")
        page = http_get_json(url)
        if not isinstance(page, list):
            raise RuntimeError(f"unexpected response for {slug}: {page}")
        if not page:
            break
        out.extend(page)
        after = page[-1].get("timestamp_created") or ""
        if len(page) < PAGE_SIZE or after[:10] < since:
            break
    return [r for r in out if (r.get("timestamp_created") or "")[:10] >= since]


def to_entries(raw: list[dict], *, normalize: bool, changelog_url_template: str | None,
               release_notes_url_template: str | None, link_template: str | None) -> list[dict]:
    """One entry per version, newest first. With `normalize`, build variants of the same base version
    collapse into one entry that records which variants exist."""
    by_key: dict[str, dict] = {}
    for rel in raw:
        ver = rel.get("version", "")
        key = base_semver(ver) if normalize else ver
        variant = ver.split("+", 1)[1] if "+" in ver else ""
        entry = by_key.get(key)
        if entry is not None:
            if variant not in entry["variants"]:
                entry["variants"].append(variant)
            continue
        date = (rel.get("timestamp_created") or "")[:10]
        prerelease = bool(rel.get("is_prerelease", False)) or not is_stable(ver, False)
        # Prefer configured PUBLIC URL templates (the API's url_changelog may point at a private
        # repo, e.g. vault-enterprise). Fall back to the API fields.
        changelog_url = (changelog_url_template.format(version=key) if changelog_url_template
                         else rel.get("url_changelog"))
        release_notes_url = (release_notes_url_template.format(version=key)
                             if release_notes_url_template else rel.get("url_release_notes"))
        by_key[key] = {
            "version": key,
            "raw_version": ver,
            "date": date,
            "date_source": "releases-api",
            "month": date[:7],
            "month_label": month_label(date),
            "is_prerelease": prerelease,
            "line": major_minor(key),
            "variants": [variant],
            "url": link_template.format(version=key) if link_template else release_notes_url,
            "changelog_url": changelog_url,
            "release_notes_url": release_notes_url,
            "highlight": None,   # filled by fetch_changelogs.py / the enrichment step
        }
    for entry in by_key.values():
        # "" = the community build. Lines that only ship +ent variants are Enterprise-only (Vault LTS).
        entry["enterprise_only"] = "" not in entry["variants"]
        entry["variants"].sort()
    return sorted(by_key.values(), key=lambda e: (e["date"], e["version"]), reverse=True)


def main() -> int:
    cfg = load_config()
    rel_cfg = cfg["releases"]
    api_base = rel_cfg["api_base"].rstrip("/")
    recent_count = int(rel_cfg.get("recent_count", 4))
    months = int(rel_cfg.get("history_months", 6))
    since = window_start(months)

    products: dict[str, dict] = {}
    for key, pcfg in rel_cfg["products"].items():
        slug = pcfg["slug"]
        log(f"fetching releases: {slug} (since {since})")
        entries = to_entries(
            fetch_raw(api_base, slug, since),
            normalize=bool(pcfg.get("normalize_base_semver", False)),
            changelog_url_template=pcfg.get("changelog_url_template"),
            release_notes_url_template=pcfg.get("release_notes_url_template"),
            link_template=pcfg.get("link_template"),
        )
        stable_only = bool(pcfg.get("stable_only", True))
        full = [e for e in entries if not (stable_only and e["is_prerelease"])]
        deck = entries if pcfg.get("deck_prereleases") else [e for e in entries if not e["is_prerelease"]]
        recent = full[:recent_count]
        latest = full[0] if full else None
        # month_window: every stable release on the SAME release line (major.minor) as the latest —
        # the set the in-place updater's newest-month column must show in full.
        line = major_minor(latest["version"]) if latest else None
        month_window = [r for r in full if line and major_minor(r["version"]) == line]
        products[key] = {
            "display": pcfg.get("display", key.title()),
            "slug": slug,
            "releases": deck,
            "latest": latest,
            "recent": recent,
            "month_window": month_window,
        }
        log(f"  {slug}: {len(deck)} releases in window "
            f"({sum(1 for e in deck if e['is_prerelease'])} pre-release); latest stable "
            f"{latest['version'] if latest else '-'}")

    tfe = cfg.get("terraform_enterprise", {})
    write_output(
        "releases.json",
        {
            "window": {"since": since, "months": months},
            "products": products,
            "terraform_enterprise": {
                "display": tfe.get("display", "Terraform Enterprise"),
                "releases_page": tfe.get("releases_page"),
                "note": "Not on the releases API; filled by fetch_tfe_releases.py.",
                "releases": [],
                "recent": [],
            },
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
