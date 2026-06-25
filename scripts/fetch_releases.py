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


def major_minor(version: str) -> str:
    """'2.0.3' -> '2.0' (the release LINE; used to scope same-line history)."""
    return ".".join(base_semver(version).split(".")[:2])


def fetch_product(
    api_base: str,
    slug: str,
    *,
    normalize: bool,
    recent_count: int,
    stable_only: bool = True,
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
        prerelease = bool(rel.get("is_prerelease", False)) or not is_stable(ver, False)
        if stable_only and prerelease:  # only drop alpha/beta/rc when the product is stable-only
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
                "is_prerelease": prerelease,
                "changelog_url": changelog_url,
                "release_notes_url": release_notes_url,
                "highlight": None,  # agent fills this in (summarized from changelog_url)
            }
        )
    # NB: we intentionally collect EVERY unique stable base in the API window (no early break).
    # main() slices the first ``recent_count`` for display and derives the same-line ``month_window``
    # from the full list — so a same-month patch (e.g. vault 2.0.2 the week before 2.0.3) is not lost
    # behind the LTS backports that crowd the top of the feed.
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
        full = fetch_product(
            api_base,
            slug,
            normalize=bool(pcfg.get("normalize_base_semver", False)),
            recent_count=recent_count,
            stable_only=bool(pcfg.get("stable_only", True)),
            changelog_url_template=pcfg.get("changelog_url_template"),
            release_notes_url_template=pcfg.get("release_notes_url_template"),
        )
        recent = full[:recent_count]
        latest = full[0] if full else None
        # month_window: every stable release in the API window on the SAME release line (major.minor)
        # as the latest. This is the authoritative set the deck's newest-month column must show in
        # full — it stops a same-month patch being silently dropped (e.g. vault 2.0.2 vs 2.0.3) and
        # gives verify_deck the ground truth for its "no skipped release" gate. LTS backports on other
        # lines (1.21.x, 1.20.x) are excluded so they never trigger a false gap.
        line = major_minor(latest["version"]) if latest else None
        month_window = [r for r in full if line and major_minor(r["version"]) == line]
        products[key] = {
            "display": pcfg.get("display", key.title()),
            "slug": slug,
            # The single newest stable release. The build agent uses this as the rolling window's
            # new col3 (right-most/latest). If its month equals the current col3 month it refreshes
            # col3 in place; if newer, the (header, entry) column pairs shift left and the oldest
            # drops off — existing col1/col2 history is preserved (it can't be re-derived: the API
            # caps at 20 items, which for Vault are all the same month's backports).
            "latest": latest,
            "recent": recent,
            "month_window": month_window,
        }
        log(f"  {slug}: recent={[r['version'] for r in recent]} "
            f"month_window={[r['version'] for r in month_window]}")

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
