#!/usr/bin/env python3
"""Fetch recent stable HashiCorp release versions from the public releases API.

Deterministic source for slides 7 (Terraform), 11/12 (Vault). Terraform Enterprise (slide 8)
is NOT on this API and is handled by the deck-releases agent from the docs releases page.

Writes data/releases.json. The one-line changelog highlight per version is intentionally left
blank ("highlight": null) for the agent to summarize from changelog_url in the deck's style.
"""
from __future__ import annotations

import calendar
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.common import http_get_json, load_config, log, write_output  # noqa: E402


def base_semver(version: str) -> str:
    """Strip build metadata: '2.0.3+ent.hsm.fips1403' -> '2.0.3'."""
    return version.split("+", 1)[0]


def month_label(date: str) -> str | None:
    """'2026-06-17' -> 'June' (bare English month name, matching the deck's column headers)."""
    if not date or len(date) < 7:
        return None
    try:
        return calendar.month_name[int(date[5:7])] or None
    except (ValueError, IndexError):
        return None


def is_stable(version: str, is_prerelease: bool) -> bool:
    if is_prerelease:
        return False
    pre = base_semver(version).lower()
    return not any(tag in pre for tag in ("alpha", "beta", "rc", "dev", "-pre"))


def fetch_product(
    api_base: str,
    slug: str,
    *,
    normalize: bool,
    recent_count: int,
    changelog_url_template: str | None = None,
    release_notes_url_template: str | None = None,
) -> list[dict]:
    # Over-fetch (API max limit is 20) because Vault returns many build variants per base version.
    url = f"{api_base}/{slug}?limit=20"
    raw = http_get_json(url)
    if not isinstance(raw, list):
        raise RuntimeError(f"unexpected response for {slug}: {raw}")

    seen: set[str] = set()
    out: list[dict] = []
    for rel in raw:
        ver = rel.get("version", "")
        if not is_stable(ver, rel.get("is_prerelease", False)):
            continue
        key = base_semver(ver) if normalize else ver
        if key in seen:
            continue
        seen.add(key)
        # Prefer configured PUBLIC URL templates (the API's url_changelog may point at a private
        # repo, e.g. vault-enterprise, which the agent cannot fetch). Fall back to the API fields.
        changelog_url = (
            changelog_url_template.format(version=key)
            if changelog_url_template
            else rel.get("url_changelog")
        )
        release_notes_url = (
            release_notes_url_template.format(version=key)
            if release_notes_url_template
            else rel.get("url_release_notes")
        )
        out.append(
            {
                "version": key,
                "raw_version": ver,
                "date": (rel.get("timestamp_created") or "")[:10],
                "month": (rel.get("timestamp_created") or "")[:7],
                "month_label": month_label((rel.get("timestamp_created") or "")[:10]),
                "is_prerelease": False,
                "changelog_url": changelog_url,
                "release_notes_url": release_notes_url,
                "highlight": None,  # agent fills this in (summarized from changelog_url)
            }
        )
        if len(out) >= recent_count:
            break
    return out


def main() -> int:
    cfg = load_config()
    rel_cfg = cfg["releases"]
    api_base = rel_cfg["api_base"].rstrip("/")
    recent_count = int(rel_cfg.get("recent_count", 4))

    products: dict[str, dict] = {}
    for key, pcfg in rel_cfg["products"].items():
        slug = pcfg["slug"]
        log(f"fetching releases: {slug}")
        recent = fetch_product(
            api_base,
            slug,
            normalize=bool(pcfg.get("normalize_base_semver", False)),
            recent_count=recent_count,
            changelog_url_template=pcfg.get("changelog_url_template"),
            release_notes_url_template=pcfg.get("release_notes_url_template"),
        )
        products[key] = {
            "display": pcfg.get("display", key.title()),
            "slug": slug,
            # The single newest stable release. The build agent uses this as the rolling window's
            # new col3 (right-most/latest). If its month equals the current col3 month it refreshes
            # col3 in place; if newer, the (header, entry) column pairs shift left and the oldest
            # drops off — existing col1/col2 history is preserved (it can't be re-derived: the API
            # caps at 20 items, which for Vault are all the same month's backports).
            "latest": recent[0] if recent else None,
            "recent": recent,
        }
        log(f"  {slug}: {[r['version'] for r in recent]}")

    # Record the TFE source so the agent knows to fetch + summarize it.
    tfe = cfg.get("terraform_enterprise", {})
    write_output(
        "releases.json",
        {
            "products": products,
            "terraform_enterprise": {
                "display": tfe.get("display", "Terraform Enterprise"),
                "releases_page": tfe.get("releases_page"),
                "note": "Not on releases API. deck-releases agent must web_fetch + summarize.",
                "recent": [],
            },
        },
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
