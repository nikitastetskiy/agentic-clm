#!/usr/bin/env python3
"""Deterministic editor for the release slides (7, 8, 11, 12).

This moves the *rule-governed* release-slide edits out of the (slow, expensive) AI build agent and
into fast, reproducible Python — while staying SAFE: anything it cannot do confidently it leaves
untouched and records as a ``fallback`` so the agent can finish that one slide.

Three cross-checkable sources of truth per entry let us reconcile human typos without guessing:
  * the visible version text   ``vX.Y.Z (YYYY-MM-DD)``
  * the entry's hyperlink       ``.../releases/tag/vX.Y.Z``  (rId -> rels Target)
  * the release data            data/releases.json, keyed by exact date

Passes (per release slide):
  1. reconcile  — if the visible version disagrees with the hyperlink/date-in-data, correct the
                  *label* (e.g. the original deck mislabels ``v1.15.4 (2026-05-27)`` whose link and
                  date are actually v1.15.5). Pure text fix; never touches the date or formatting.
  2. refresh    — if ``products.<p>.latest`` belongs to the newest (rightmost) column's month and is
                  not yet shown, replace that column's prerelease/older top entry with the latest
                  (version + date + highlight) and repoint its changelog hyperlink.
  3. shift      — only when latest is a *newer* month than col3 AND every column holds exactly one
                  entry (the safe, unambiguous case): relabel headers and move each single entry one
                  column left, writing the latest into col3. Multi-entry shifts -> fallback.

Anything outside these safe shapes (split version runs, multi-entry shift, unexpected geometry) is
left as-is and reported as fallback. The deterministic verify_deck.py gate validates the result.

Usage:
    python scripts/edit_release_slides.py "<working-copy>.pptx" [--report data/release-edit-report.json]
                                          [--dry-run] [--original "<orig>.pptx"]
Operates IN PLACE on the working copy (never the original). Exit 0 always (fallbacks are normal);
exit 2 only on hard error.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from xml.dom import minidom

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.common import DATA_DIR, REPO_ROOT, load_config, log  # noqa: E402
from lib.pptx_edit import prune_orphan_hyperlinks  # noqa: E402

UNPACK = REPO_ROOT / ".github/skills/pptx/scripts/office/unpack.py"
PACK = REPO_ROOT / ".github/skills/pptx/scripts/office/pack.py"
EMU = 914400
COL_BOUNDS = [(0.0, 2.0), (2.0, 5.0), (5.0, 12.0)]  # x-buckets for the 3 columns
HEADER_Y_MAX = 1.5
VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
TAG_VER_RE = re.compile(r"/tag/v?(\d+\.\d+\.\d+)")

MONTHS = ["January", "February", "March", "April", "May", "June",
          "July", "August", "September", "October", "November", "December"]


def _rin(v: str | None) -> float | None:
    try:
        return round(int(v) / EMU, 2)
    except (TypeError, ValueError):
        return None


def _t(node) -> str:
    return "".join(t.firstChild.data for t in node.getElementsByTagName("a:t") if t.firstChild)


# --------------------------------------------------------------------------- release data ---------

def load_release_index() -> dict:
    """Return {product_key: {'latest': {...}, 'by_date': {date: entry}, 'versions': set, 'tag_base': url}}.

    product_key matches slide-map's ``product`` field: terraform, vault, terraform-enterprise.
    """
    data = json.loads((DATA_DIR / "releases.json").read_text(encoding="utf-8"))
    idx: dict[str, dict] = {}
    for key, prod in data.get("products", {}).items():
        entries = list(prod.get("recent", []))
        latest = prod.get("latest")
        if latest:
            entries = [latest] + [e for e in entries if e.get("date") != latest.get("date")]
        by_date = {e["date"]: e for e in entries if e.get("date")}
        idx[key] = {
            "latest": latest,
            "by_date": by_date,
            "versions": {e.get("version") for e in entries if e.get("version")},
            "entries": entries,
        }
    te = data.get("terraform_enterprise")
    if te:
        latest = te.get("latest")
        entries = ([latest] if latest else []) + list(te.get("recent", []))
        by_date = {e["date"]: e for e in entries if e.get("date")}
        idx["terraform-enterprise"] = {
            "latest": latest,
            "by_date": by_date,
            "versions": {e.get("version") for e in entries if e.get("version")},
            "entries": entries,
        }
    return idx


def tag_url(product: str, version: str) -> str:
    repo = {"terraform": "terraform", "terraform-enterprise": "terraform",
            "vault": "vault"}.get(product, product)
    return f"https://github.com/hashicorp/{repo}/releases/tag/v{version}"


# --------------------------------------------------------------------------- slide model ----------

class Entry:
    """A single release entry: its version paragraph (+ following highlight paragraphs)."""

    def __init__(self, version_para, version_at, text: str):
        self.version_para = version_para
        self.version_at = version_at          # the <a:t> node holding "vX.Y.Z (date)"
        self.text = text
        self.highlight_paras: list = []       # bullet paragraphs until the next version line
        m = VERSION_RE.search(text)
        d = DATE_RE.search(text)
        self.version = m.group(0) if m else None
        self.date = d.group(1) if d else None

    @property
    def hlink_rid(self) -> str | None:
        for r in self.version_para.getElementsByTagName("a:hlinkClick"):
            rid = r.getAttribute("r:id")
            if rid:
                return rid
        return None


class Column:
    def __init__(self, idx: int):
        self.idx = idx
        self.header_at = None      # the <a:t> of the month header
        self.header_text = None
        self.entry_shape = None    # the <a:txBody>'s parent <p:sp> holding the entry paragraphs
        self.entries: list[Entry] = []

    @property
    def header_month(self) -> str | None:
        if not self.header_text:
            return None
        for mth in MONTHS:
            if mth.lower() in self.header_text.lower():
                return mth
        return None


def parse_slide(slide_dom) -> list[Column]:
    cols = [Column(i) for i in range(3)]
    for sp in slide_dom.getElementsByTagName("p:sp"):
        off = sp.getElementsByTagName("a:off")
        if not off:
            continue
        x = _rin(off[0].getAttribute("x"))
        y = _rin(off[0].getAttribute("y"))
        if x is None or y is None:
            continue
        col = next((i for i, (lo, hi) in enumerate(COL_BOUNDS) if lo <= x < hi), None)
        if col is None or y < 1.2:
            continue
        paras = [p for p in sp.getElementsByTagName("a:p")]
        text_all = _t(sp).strip()
        if y < HEADER_Y_MAX and not VERSION_RE.search(text_all):
            ats = sp.getElementsByTagName("a:t")
            if ats and ats[0].firstChild:
                cols[col].header_at = ats[0]
                cols[col].header_text = text_all
            continue
        # entry-block shape
        cols[col].entry_shape = sp
        current: Entry | None = None
        for p in paras:
            ptxt = _t(p).strip()
            if not ptxt:
                continue
            if VERSION_RE.search(ptxt) and DATE_RE.search(ptxt):
                at = next((t for t in p.getElementsByTagName("a:t")
                           if t.firstChild and VERSION_RE.search(t.firstChild.data)), None)
                if at is None:
                    continue
                current = Entry(p, at, ptxt)
                cols[col].entries.append(current)
            elif current is not None:
                current.highlight_paras.append(p)
    return cols


# --------------------------------------------------------------------------- rels helpers ---------

def load_rels(rels_file: Path) -> tuple[minidom.Document, dict]:
    dom = minidom.parse(str(rels_file))
    rid_target = {r.getAttribute("Id"): r.getAttribute("Target")
                  for r in dom.getElementsByTagName("Relationship")}
    return dom, rid_target


HYPERLINK_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink"


def add_rel(rels_dom, target: str) -> str:
    """Append a NEW external-hyperlink relationship and return its rId.

    We never mutate an existing relationship's Target because rIds are frequently SHARED across
    entries (e.g. slide 11 col2 and col3 both used rId6); repointing one would corrupt the other.
    """
    container = rels_dom.getElementsByTagName("Relationships")[0]
    nums = [int(r.getAttribute("Id")[3:]) for r in rels_dom.getElementsByTagName("Relationship")
            if r.getAttribute("Id").startswith("rId") and r.getAttribute("Id")[3:].isdigit()]
    new_id = f"rId{(max(nums) + 1) if nums else 1}"
    rel = rels_dom.createElement("Relationship")
    rel.setAttribute("Id", new_id)
    rel.setAttribute("Type", HYPERLINK_TYPE)
    rel.setAttribute("Target", target)
    rel.setAttribute("TargetMode", "External")
    container.appendChild(rel)
    return new_id


def relink_version(entry: "Entry", rels_dom, product: str, version: str) -> None:
    """Point the entry's VERSION RUN hyperlink at v<version>'s changelog tag via a FRESH rel.

    Scoped to the run that holds the version <a:t> (not endParaRPr / highlight runs), and uses a new
    relationship so shared rIds elsewhere are unaffected. No-op if the version run has no hyperlink.
    """
    run = entry.version_at.parentNode  # <a:r>
    hlinks = run.getElementsByTagName("a:hlinkClick")
    if not hlinks:
        return
    new_rid = add_rel(rels_dom, tag_url(product, version))
    hlinks[0].setAttribute("r:id", new_rid)


# --------------------------------------------------------------------------- text edits -----------

def set_at(at_node, text: str) -> None:
    at_node.firstChild.data = text


def set_highlight(entry: Entry, highlight: str) -> bool:
    """Put ``highlight`` into the entry's first highlight paragraph; collapse extra runs.

    Keeps the paragraph's pPr (bullet/indent) and the first run's rPr (font/size). Returns False if
    there is no highlight paragraph to write into (caller decides whether that's a fallback)."""
    if not entry.highlight_paras:
        return False
    para = entry.highlight_paras[0]
    runs = para.getElementsByTagName("a:r")
    if not runs:
        return False
    first = runs[0]
    at = next((t for t in first.getElementsByTagName("a:t")), None)
    if at is None or at.firstChild is None:
        return False
    at.firstChild.data = highlight
    for extra in runs[1:]:
        extra.parentNode.removeChild(extra)
    # drop any further highlight paragraphs for this entry (single-line highlight)
    for extra_para in entry.highlight_paras[1:]:
        if extra_para.parentNode:
            extra_para.parentNode.removeChild(extra_para)
    return True


# --------------------------------------------------------------------------- passes ---------------

def reconcile(cols: list[Column], rel_index: dict, rels_dom, rid_target: dict,
              product: str, report: dict) -> None:
    """Correct mislabeled version numbers — but ONLY when the API data (keyed by exact date) is the
    authority. The hyperlink is a corroborating signal, never the sole basis for a change (a link can
    itself be a copy-paste error, e.g. slide 11 col3 text=2.0.2 / link=2.0.1 with no data for that
    date — that is left untouched and merely noted)."""
    by_date = rel_index.get("by_date", {})
    for col in cols:
        for e in col.entries:
            if not e.version or not e.date:
                continue
            rid = e.hlink_rid
            link_ver = None
            if rid and rid in rid_target:
                m = TAG_VER_RE.search(rid_target[rid])
                link_ver = m.group(1) if m else None
            data_ver = (by_date.get(e.date) or {}).get("version")
            if not data_ver:
                # outside the fetched window -> no external truth; just flag text/link disagreement
                if link_ver and link_ver != e.version:
                    report["notes"].append(
                        f"col{col.idx+1} {e.version}@{e.date}: text/link disagree (link={link_ver}), "
                        "no data for date -> left as-is")
                continue
            if e.version != data_ver:
                old_ver = e.version
                new_text = VERSION_RE.sub(data_ver, e.text, count=1)
                set_at(e.version_at, new_text)
                e.version = data_ver
                if rid:
                    relink_version(e, rels_dom, product, data_ver)
                report["reconciled"].append({"col": col.idx + 1, "date": e.date,
                                             "from": old_ver, "to": data_ver, "text": new_text.strip()})
                report["changed"] = True
            elif link_ver and link_ver != data_ver:
                # text already correct, but the hyperlink points at the wrong tag -> fix the link
                if rid:
                    relink_version(e, rels_dom, product, data_ver)
                    report["notes"].append(f"col{col.idx+1} {data_ver}@{e.date}: repointed link {link_ver}->{data_ver}")
                    report["changed"] = True


def ensure_latest(cols: list[Column], rel_index: dict, rels_dom, rid_target: dict,
                  product: str, report: dict) -> None:
    """Refresh (same month) or shift (new month) so the rightmost column carries ``latest``."""
    latest = rel_index.get("latest")
    if not latest or not latest.get("version"):
        report["notes"].append("no latest in data; skipped")
        return
    lv, ld, lhl = latest["version"], latest.get("date", ""), latest.get("highlight", "")
    lmonth = latest.get("month_label")
    col3 = cols[2]
    shown = {e.version for c in cols for e in c.entries}
    if lv in shown:
        report["notes"].append(f"latest {lv} already present; no shift/refresh needed")
        return
    col3_month = col3.header_month or (
        MONTHS[int(col3.entries[0].date[5:7]) - 1] if col3.entries and col3.entries[0].date else None)

    if lmonth and col3_month and lmonth == col3_month and col3.entries:
        # REFRESH: replace col3's top entry (prerelease/older) with latest
        top = col3.entries[0]
        set_at(top.version_at, f"v{lv} ({ld})")
        if not set_highlight(top, lhl):
            report["fallback"] = True
            report["notes"].append("refresh: could not set highlight; fallback")
            return
        relink_version(top, rels_dom, product, lv)
        report["refreshed"] = {"version": lv, "col": 3}
        report["changed"] = True
        return

    # NEW MONTH -> shift. Only safe when every column has exactly one entry.
    if all(len(c.entries) == 1 for c in cols) and all(c.header_at for c in cols):
        # relabel headers: drop col1's month, slide col2->col1, col3->col2, new month into col3
        new_headers = [cols[1].header_text, cols[2].header_text, lmonth or col3_month]
        old = [c.entries[0] for c in cols]
        # write text: col1 <- old col2, col2 <- old col3, col3 <- latest
        for dst, src in ((cols[0], old[1]), (cols[1], old[2])):
            set_at(dst.entries[0].version_at, src.text)
            if src.highlight_paras and dst.entries[0].highlight_paras:
                set_highlight(dst.entries[0], _t(src.highlight_paras[0]).strip())
            if src.version:
                relink_version(dst.entries[0], rels_dom, product, src.version)
        # col3 <- latest
        set_at(cols[2].entries[0].version_at, f"v{lv} ({ld})")
        set_highlight(cols[2].entries[0], lhl)
        relink_version(cols[2].entries[0], rels_dom, product, lv)
        for col, htext in zip(cols, new_headers):
            if col.header_at and htext:
                set_at(col.header_at, htext)
        report["shifted"] = {"version": lv, "headers": new_headers}
        report["changed"] = True
        return

    report["fallback"] = True
    report["notes"].append(
        f"latest {lv} is a new month ({lmonth}) requiring a multi-entry shift; left for agent")


# --------------------------------------------------------------------------- driver ---------------

def edit_unpacked(unpacked: Path, slide_map: dict, rel_index: dict) -> dict:
    report = {"slides": {}, "any_fallback": False, "any_change": False}
    for entry in slide_map.get("slides", []):
        if entry.get("type") != "release_updates":
            continue
        product = entry.get("product")
        ridx = rel_index.get(product)
        ns = entry["n"]
        for n in (ns if isinstance(ns, list) else [ns]):
            slide_file = unpacked / "ppt" / "slides" / f"slide{n}.xml"
            rels_file = unpacked / "ppt" / "slides" / "_rels" / f"slide{n}.xml.rels"
            if not slide_file.exists() or not ridx:
                continue
            srep = {"product": product, "reconciled": [], "notes": [],
                    "refreshed": None, "shifted": None, "fallback": False, "changed": False}
            dom = minidom.parse(str(slide_file))
            rels_dom, rid_target = load_rels(rels_file)
            cols = parse_slide(dom)
            reconcile(cols, ridx, rels_dom, rid_target, product, srep)
            ensure_latest(cols, ridx, rels_dom, rid_target, product, srep)
            if srep["changed"]:
                pruned = prune_orphan_hyperlinks(dom, rels_dom)
                if pruned:
                    srep["pruned_rels"] = pruned
                slide_file.write_text(dom.toxml(), encoding="utf-8")
                rels_file.write_text(rels_dom.toxml(), encoding="utf-8")
            report["slides"][str(n)] = srep
            report["any_fallback"] = report["any_fallback"] or srep["fallback"]
            report["any_change"] = report["any_change"] or srep["changed"]
    # Convenience lists for the build agent: which release slides it must still do vs may skip.
    report["fallback_slides"] = sorted(
        (int(n) for n, s in report["slides"].items() if s["fallback"]), )
    report["handled_slides"] = sorted(
        (int(n) for n, s in report["slides"].items() if not s["fallback"]), )
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("deck", help="working-copy .pptx (edited in place)")
    ap.add_argument("--report", default=str(DATA_DIR / "release-edit-report.json"))
    ap.add_argument("--original", default=None, help="guard: refuse if deck == original")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    deck = Path(args.deck).resolve()
    if not deck.exists():
        raise SystemExit(f"deck not found: {deck}")
    if args.original and Path(args.original).resolve() == deck:
        raise SystemExit("refusing to edit the ORIGINAL deck in place")

    slide_map = load_config("slide-map.yaml")
    rel_index = load_release_index()

    tmp = Path(tempfile.mkdtemp(prefix="reledit-"))
    try:
        subprocess.run([sys.executable, str(UNPACK), str(deck), str(tmp)], check=True, capture_output=True)
        report = edit_unpacked(tmp, slide_map, rel_index)
        if report["any_change"] and not args.dry_run:
            out = subprocess.run(
                [sys.executable, str(PACK), str(tmp), str(deck), "--validate", "true"],
                capture_output=True, text=True)
            if out.returncode != 0:
                log(f"pack failed: {out.stderr[-500:]}")
                return 2
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    Path(args.report).write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    for n, s in report["slides"].items():
        bits = []
        if s["reconciled"]:
            bits.append(f"reconciled={[r['to'] for r in s['reconciled']]}")
        if s["refreshed"]:
            bits.append(f"refreshed={s['refreshed']['version']}")
        if s["shifted"]:
            bits.append(f"shifted->{s['shifted']['version']}")
        if s["fallback"]:
            bits.append("FALLBACK(agent)")
        log(f"slide {n} ({s['product']}): {', '.join(bits) or 'no change'}")
        for note in s["notes"]:
            log(f"    - {note}")
    log(f"report -> {args.report}"
        + ("  [DRY-RUN, deck not written]" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
