#!/usr/bin/env python3
"""Deterministic post-build verifier for the cadence deck.

This is the "stop doing it by hand" automation: it machine-checks a produced deck the CORRECT way so
that neither a human nor the AI reviewer has to eyeball slide XML (two mistakes this replaces: the
reviewer's false hide-FAIL from checking presentation.xml instead of the slide root, and a human
mis-reading truncated column text). It runs in run.sh after the build (a cheap, exact gate) and the
AI reviewer reads its report and focuses on the things only judgment can assess (style nuance, prose).

Checks (all deterministic, no AI):
  1. Integrity      — output is a valid deck and (vs --original) has the SAME slide count (nothing deleted).
  2. Hide audit     — which slides are hidden, via <p:sld ... show="0"> on the SLIDE ROOT (the real
                      PowerPoint "Hide Slide" flag), mapped to display positions. Flags event/webinar/
                      featured slides that are still VISIBLE while their events.json status is unavailable
                      (the actual risk: shipping past-dated events).
  3. Style          — runs the hashicorp-deck-style style_check.py on the in-scope slides.
  4. Release cols   — for each release slide, reports each column's header + entry count and asserts the
                      latest (rightmost) column carries products.<product>.latest.version from releases.json.
  5. Release accuracy — date-keyed: every shown entry "vX.Y.Z (YYYY-MM-DD)" must match releases.json for
                      that exact DATE (the authoritative key). Catches mislabels the col3-only check missed
                      (e.g. an entry dated 2026-05-27 typed as 1.15.4 when the API says that date is 1.15.5).

Usage:
    python scripts/verify_deck.py "output/<deck>.pptx" [--original "<original>.pptx"] [--json]

Exit code 0 if every check PASSes, 1 otherwise. Pass --json for a machine-readable summary on stdout.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from xml.dom import minidom

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.common import DATA_DIR, REPO_ROOT, load_config, log  # noqa: E402

UNPACK = REPO_ROOT / ".github/skills/pptx/scripts/office/unpack.py"
STYLE_CHECK = REPO_ROOT / ".github/skills/hashicorp-deck-style/scripts/style_check.py"
EMU_PER_IN = 914400
COL_BOUNDS = [(0.0, 2.0), (2.0, 5.0), (5.0, 12.0)]  # x-buckets for the 3 release columns
HEADER_Y_MAX = 1.5  # y below this on a column = month header; above = release entry
SLD_ROOT_RE = re.compile(r"<p:sld\b[^>]*>")
VERSION_RE = re.compile(r"v?(\d+\.\d+\.\d+)")
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
# slide-map product name -> releases.json location of that product's {latest, recent[]}
PRODUCT_KEY = {"terraform": ("products", "terraform"), "vault": ("products", "vault"),
               "terraform-enterprise": ("terraform_enterprise", None)}


def _unpack(deck: Path) -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="verify-"))
    subprocess.run([sys.executable, str(UNPACK), str(deck), str(tmp)], check=True, capture_output=True)
    return tmp


def _text(node) -> str:
    return "".join(t.firstChild.data for t in node.getElementsByTagName("a:t") if t.firstChild)


def slide_order(unpacked: Path) -> list[Path]:
    """Resolve display order (1-based) -> slideN.xml file, via presentation.xml + rels."""
    pres = unpacked / "ppt" / "presentation.xml"
    rels = unpacked / "ppt" / "_rels" / "presentation.xml.rels"
    rid_to_target = {
        r.getAttribute("Id"): r.getAttribute("Target")
        for r in minidom.parse(str(rels)).getElementsByTagName("Relationship")
    }
    order: list[Path] = []
    lst = minidom.parse(str(pres)).getElementsByTagName("p:sldIdLst")
    if not lst:
        return order
    for sld in lst[0].getElementsByTagName("p:sldId"):
        target = rid_to_target.get(sld.getAttribute("r:id"), "")
        order.append((unpacked / "ppt" / target.replace("../", "")).resolve())
    return order


def is_hidden(slide_file: Path) -> bool:
    """True iff the slide ROOT carries show="0" (PowerPoint's real Hide-Slide flag).

    The flag lives on <p:sld ... show="0">, which is NOT the first '>' in the file (that is the
    <?xml?> declaration) — so locate the slide-root tag explicitly.
    """
    m = SLD_ROOT_RE.search(slide_file.read_text(encoding="utf-8")[:2000])
    return bool(m and 'show="0"' in m.group(0))


def _round_in(v: str | None) -> float | None:
    try:
        return round(int(v) / EMU_PER_IN, 2)
    except (TypeError, ValueError):
        return None


def release_columns(slide_file: Path) -> dict[int, dict]:
    """Group release-slide shapes into 3 columns -> {header, version_entries[]}.

    A "version entry" is any paragraph containing a vX.Y.Z token (entries may be prefixed, e.g.
    "Terraform Enterprise v2.0.3 (...)"). The bare semver is stored for comparison.
    """
    doc = minidom.parse(str(slide_file))
    cols: dict[int, dict] = {i: {"header": None, "entries": []} for i in range(3)}
    for sp in doc.getElementsByTagName("p:sp"):
        off = sp.getElementsByTagName("a:off")
        if not off:
            continue
        x = _round_in(off[0].getAttribute("x"))
        y = _round_in(off[0].getAttribute("y"))
        if x is None or y is None or y < 1.2:
            continue
        col = next((i for i, (lo, hi) in enumerate(COL_BOUNDS) if lo <= x < hi), None)
        if col is None:
            continue
        for p in sp.getElementsByTagName("a:p"):
            txt = _text(p).strip()
            if not txt:
                continue
            m = VERSION_RE.search(txt)
            if m:
                cols[col]["entries"].append(m.group(1))
            elif y < HEADER_Y_MAX and cols[col]["header"] is None:
                cols[col]["header"] = txt
    return cols


def slide_dated_entries(slide_file: Path) -> list[tuple[str, str]]:
    """Every release entry on a slide as (semver, 'YYYY-MM-DD'), for date-keyed accuracy checks.

    A release entry paragraph looks like "v1.15.5 (2026-05-27)" or
    "Terraform Enterprise v2.0.3 (2026-06-17)" — one version + one date in the same paragraph.
    """
    doc = minidom.parse(str(slide_file))
    out: list[tuple[str, str]] = []
    for sp in doc.getElementsByTagName("p:sp"):
        off = sp.getElementsByTagName("a:off")
        if not off:
            continue
        y = _round_in(off[0].getAttribute("y"))
        if y is None or y < 1.2:
            continue
        for p in sp.getElementsByTagName("a:p"):
            txt = _text(p).strip()
            mv, md = VERSION_RE.search(txt), DATE_RE.search(txt)
            if mv and md:
                out.append((mv.group(1), md.group(1)))
    return out


def release_by_date(releases: dict) -> dict[str, dict[str, str]]:
    """Build {product_key: {'YYYY-MM-DD': version}} from releases.json latest + recent[]."""
    by_date: dict[str, dict[str, str]] = {}
    for product, (top, sub) in PRODUCT_KEY.items():
        node = releases.get(top, {})
        node = node.get(sub, {}) if sub else node
        dates: dict[str, str] = {}
        for rec in [node.get("latest")] + list(node.get("recent", [])):
            if rec and rec.get("date") and rec.get("version"):
                dates.setdefault(rec["date"], rec["version"])
        by_date[product] = dates
    return by_date


def _load_json(name: str) -> dict:
    p = DATA_DIR / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def _count_slides(deck: Path) -> int:
    with zipfile.ZipFile(deck) as z:
        return sum(1 for n in z.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("deck")
    ap.add_argument("--original", default=None)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    deck = Path(args.deck).resolve()
    if not deck.exists():
        raise SystemExit(f"deck not found: {deck}")
    original = Path(args.original).resolve() if args.original else None
    cfg = load_config("slide-map.yaml")
    releases = _load_json("releases.json")
    events = _load_json("events.json")

    results: dict[str, dict] = {}
    unpacked = _unpack(deck)
    try:
        order = slide_order(unpacked)
        total = len(order)

        # --- 1. Integrity ---------------------------------------------------------------
        integ = {"slides": total, "ok": True, "notes": []}
        if original and original.exists():
            o = _count_slides(original)
            integ["original_slides"] = o
            if o != total:
                integ["ok"] = False
                integ["notes"].append(f"slide count changed: original {o} -> output {total} (deletion/addition!)")
            if original == deck:
                integ["ok"] = False
                integ["notes"].append("output path == original path (original would be overwritten)")
        results["integrity"] = integ

        # --- 2. Hide audit --------------------------------------------------------------
        hidden = {i + 1 for i, f in enumerate(order) if is_hidden(f)}
        # event/webinar/featured slides that SHOULD be hidden when events are unavailable/empty
        event_types = {"webinars", "in_person_event"}
        event_positions: list[int] = []
        for entry in cfg.get("slides", []):
            if entry.get("type") in event_types:
                ns = entry["n"]
                event_positions += ns if isinstance(ns, list) else [ns]
        events_unavailable = events.get("status") == "unavailable" or not (
            events.get("webinars") or events.get("featured")
        )
        stale_visible = sorted(p for p in event_positions if events_unavailable and p not in hidden)
        hide = {
            "hidden_positions": sorted(hidden),
            "events_unavailable": events_unavailable,
            "event_slides": sorted(set(event_positions)),
            "stale_visible": stale_visible,
            "ok": not stale_visible,
            "notes": ([f"event slides VISIBLE despite unavailable events (past-dated risk): {stale_visible}"] if stale_visible else []),
        }
        results["hide_audit"] = hide

        # --- 3. Style -------------------------------------------------------------------
        in_scope = []
        for entry in cfg.get("slides", []):
            ns = entry["n"]
            in_scope += ns if isinstance(ns, list) else [ns]
        in_scope = [str(n) for n in sorted(set(in_scope))]
        proc = subprocess.run(
            [sys.executable, str(STYLE_CHECK), str(unpacked), *in_scope],
            capture_output=True, text=True,
        )
        results["style"] = {
            "ok": proc.returncode == 0,
            "exit": proc.returncode,
            "notes": ([proc.stdout.strip().splitlines()[-1]] if proc.returncode != 0 and proc.stdout else []),
        }

        # --- 4. Release columns ---------------------------------------------------------
        rel = {"slides": {}, "ok": True, "notes": []}
        prod_latest = {
            k: (v.get("latest") or {}).get("version")
            for k, v in releases.get("products", {}).items()
        }
        prod_latest["terraform-enterprise"] = (releases.get("terraform_enterprise", {}).get("latest") or {}).get("version")
        for entry in cfg.get("slides", []):
            if entry.get("type") != "release_updates":
                continue
            ns = entry["n"]
            for n in (ns if isinstance(ns, list) else [ns]):
                f = unpacked / "ppt" / "slides" / f"slide{n}.xml"
                if not f.exists():
                    continue
                cols = release_columns(f)
                product = entry.get("product")
                latest_v = prod_latest.get(product)
                col3 = cols[2]
                col3_has_latest = bool(latest_v) and latest_v in col3["entries"]
                summary = {
                    "headers": [cols[i]["header"] for i in range(3)],
                    "entry_counts": [len(cols[i]["entries"]) for i in range(3)],
                    "col3_entries": col3["entries"],
                    "expected_latest": latest_v,
                    "col3_has_latest": col3_has_latest,
                }
                rel["slides"][str(n)] = summary
                if latest_v and not col3_has_latest:
                    rel["ok"] = False
                    rel["notes"].append(f"slide {n} ({product}): latest col missing {latest_v} (has {col3['entries']})")
        results["release_columns"] = rel

        # --- 5. Release accuracy (date-keyed) -------------------------------------------
        # Catches the mislabel class (e.g. an entry dated 2026-05-27 typed as 1.15.4 when the API
        # says that date is 1.15.5). The exact release DATE is the authoritative key; the printed
        # version must agree with releases.json for that date. This is the guard that the original
        # col3-only check lacked — it would have caught the v1.15.5 typo on slide 7.
        acc = {"slides": {}, "ok": True, "notes": []}
        by_date = release_by_date(releases)
        for entry in cfg.get("slides", []):
            if entry.get("type") != "release_updates":
                continue
            product = entry.get("product")
            dates = by_date.get(product, {})
            if not dates:
                continue
            ns = entry["n"]
            for n in (ns if isinstance(ns, list) else [ns]):
                f = unpacked / "ppt" / "slides" / f"slide{n}.xml"
                if not f.exists():
                    continue
                mismatches = []
                for ver, date in slide_dated_entries(f):
                    expected = dates.get(date)
                    if expected and expected != ver:
                        mismatches.append(f"{date}: shows {ver}, data says {expected}")
                acc["slides"][str(n)] = mismatches
                if mismatches:
                    acc["ok"] = False
                    for m in mismatches:
                        acc["notes"].append(f"slide {n} ({product}): {m}")
        results["release_accuracy"] = acc
    finally:
        shutil.rmtree(unpacked, ignore_errors=True)

    overall = all(r.get("ok", True) for r in results.values())
    results["overall"] = {"ok": overall}

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        print(f"\n=== verify_deck: {deck.name} ===")
        for name, r in results.items():
            if name == "overall":
                continue
            status = "PASS" if r.get("ok", True) else "FAIL"
            print(f"[{status}] {name}")
            for note in r.get("notes", []):
                print(f"        - {note}")
        ha = results["hide_audit"]
        print(f"        hidden positions: {ha['hidden_positions']}")
        for n, s in results["release_columns"]["slides"].items():
            print(f"        slide {n}: headers={s['headers']} entries={s['entry_counts']} latest={s['expected_latest']} ok={s['col3_has_latest']}")
        print(f"\n=== OVERALL: {'PASS' if overall else 'FAIL'} ===")
    return 0 if overall else 1


if __name__ == "__main__":
    raise SystemExit(main())
