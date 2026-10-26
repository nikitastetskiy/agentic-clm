#!/usr/bin/env python3
"""Deterministic gate for a BUILT weekly deck: read the .pptx back and prove it says what it should.

    python scripts/verify_build.py [deck.pptx]      # default: the deck named in data/build-report.json

The builder writes a deck and a report of what it wrote; this script trusts neither. It unpacks the
deck, reads every automated slide back out of the XML with the same structure finders the builder
uses, and checks it against three independent things — the plan, the fetched data and the template:

  package      the file opens; every slide carries a role; none is hidden; roles are in plan order
  columns      each month column shows exactly the planned entries (heading, links, wording) newest
               first, in the right month — and the newest release in the DATA is really on the slide
  blogs        the planned posts, newest first, each linked to its own URL, none twice
  events       the planned cards / featured slides; nothing dated before the build day; no card
               without a description
  fit          no column, card or blog title is predicted to overflow its box (measured again from
               the deck itself, not taken from the build report)
  layout       nothing extends past the slide's edges; event cards, their flags and product icons sit
               on their lines; month headers are level; the blog grid is square; two highlights of
               one entry never run together (the rules in lib/layout.py)
  links        every hyperlink resolves to a relationship, is http(s), and does not point at an
               internal host (w3, GitHub Enterprise, Box, Slack …) — this deck goes to customers
  disclosure   every CVE id in the deck is named by the public release notes that were fetched
  formatting   no colour or typeface that the template does not already use, and no embedded font
               whose data is another font than its name
  freshness    the cover shows the build month and does not claim the deck is ready to publish

Exit 0 only if every check passes. Writes data/verify-report.json (machine-readable) and prints a
PASS/FAIL line per check, with the reasons for each failure.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import urlparse
from xml.dom import minidom

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import compose, deck, fonts, layout, slides as sl, textfit  # noqa: E402
from lib import pptx_edit as px  # noqa: E402
from lib.common import DATA_DIR, MONTH_NAMES, REPO_ROOT, load_config, log, read_json  # noqa: E402
from lib.textfit import EMU_PT  # noqa: E402

CVE_RE = re.compile(r"CVE-\d{4}-\d{4,7}", re.I)
# Hosts that only resolve inside the company, or that hold internal material.
INTERNAL_HOST_RE = re.compile(
    r"(^|\.)(w3\.ibm\.com|github\.ibm\.com|pages\.github\.ibm\.com|box\.com|slack\.com|"
    r"sharepoint\.com|atlassian\.net|localhost)$|^(10|127|192\.168|172\.(1[6-9]|2\d|3[01]))\.", re.I)
FIT_TOLERANCE = 1.02        # the builder aims below 100 %; allow rounding


def norm(text: str) -> str:
    return " ".join((text or "").replace(" ", " ").split())


class Check:
    def __init__(self, name: str):
        self.name = name
        self.notes: list[str] = []
        self.info: list[str] = []

    def fail(self, note: str) -> None:
        self.notes.append(note)

    @property
    def ok(self) -> bool:
        return not self.notes


def slide_text(dom) -> str:
    return " ".join(t.firstChild.data for t in dom.getElementsByTagName("a:t") if t.firstChild)


def body_height_pt(sp, paragraphs=None) -> float:
    """Predicted height of a text box's paragraphs, from the paragraphs' own formatting."""
    left, _t, right, _b = compose.insets(sp)
    width = (sl.box(sp)[2] - left - right) / EMU_PT
    total = 0.0
    for p in (paragraphs if paragraphs is not None else px.paragraphs(sp)):
        segs = [(px.run_text(r), sl.is_bold(r)) for r in px.para_runs(p)]
        if not any(t.strip() for t, _ in segs):
            continue
        total += textfit.para_height(segs, compose.style_of(p, default_size=6.5), width)
    return total


# --------------------------------------------------------------------------- per-kind checks ------
def check_columns(checks: dict, role: str, dom, rid_target: dict, plan: dict, rule: dict,
                  data: dict) -> None:
    c, fit = checks["columns"], checks["fit"]
    cols = sl.columns(dom)
    if len(cols) != len(plan["columns"]):
        c.fail(f"{role}: {len(cols)} month columns on the slide, {len(plan['columns'])} planned")
        return
    for col, cplan in zip(cols, plan["columns"]):
        where = f"{role} / {cplan['header']}"
        if sl.header_text(col["header"]) != cplan["header"]:
            c.fail(f"{where}: header reads '{sl.header_text(col['header'])}'")
        shown = sl.read_entries(col["body"], rid_target)
        planned = cplan["entries"]
        # The builder may trim the OLDEST entries of an overfull month; what is shown must still be
        # the newest ones, in order, each with wording the plan offered.
        if len(shown) > len(planned) or not shown:
            c.fail(f"{where}: {len(shown)} entries on the slide, {len(planned)} planned")
            continue
        if len(shown) < len(planned):
            c.info.append(f"{where}: {len(planned) - len(shown)} oldest entries trimmed to fit")
        for got, want in zip(shown, planned):
            heading = norm("".join(seg["text"] for seg in want["heading"]))
            if norm(got["heading"]) != heading:
                c.fail(f"{where}: heading '{got['heading']}' != planned '{heading}'")
                continue
            urls = [seg["url"] for seg in want["heading"] if seg.get("url")]
            if got["links"] != list(dict.fromkeys(urls)):
                c.fail(f"{where}: {heading} links to {got['links']}, planned {urls}")
            bullets = [norm(sl.strip_markup(b)) for b in got["bullets"]]
            options = [[norm(sl.strip_markup(b)) for b in opt] for opt in want["options"]]
            if bullets not in options and not (len(shown) < len(planned) and bullets[:-1] in options):
                c.fail(f"{where}: {heading} wording is not one of the planned options: {bullets}")
            if want["date"][:7] != cplan["month"]:
                c.fail(f"{where}: {heading} is dated {want['date']}, outside the column's month")
        dates = [e["date"] for e in planned[:len(shown)]]
        if dates != sorted(dates, reverse=True):
            c.fail(f"{where}: entries are not newest-first")
        used = body_height_pt(col["body"])
        _l, top, _r, bottom = compose.insets(col["body"])
        room = (sl.box(col["body"])[3] - top - bottom) / EMU_PT
        if used > room * FIT_TOLERANCE:
            fit.fail(f"{where}: text needs ~{used:.0f} pt, the box holds {room:.0f} pt")
    # Independent of the plan: is the newest thing the SOURCE has really on the slide?
    source = rule.get("source") or {}
    newest_col = sl.read_entries(cols[-1]["body"], rid_target)
    text = " ".join(e["heading"] for e in newest_col)
    if source.get("type") == "releases":
        node = (data["releases"].get("terraform_enterprise") if source["product"] == "terraform-enterprise"
                else (data["releases"].get("products") or {}).get(source["product"])) or {}
        newest = (node.get("releases") or [{}])[0]
        shown_version = re.sub(r"-alpha\d{8}$", "-alpha", newest.get("version", ""))
        if not newest or shown_version not in text or newest.get("date", "") not in text:
            c.fail(f"{role}: newest release in the data ({newest.get('version')}, {newest.get('date')}) "
                   "is not in the newest column")
    elif source.get("type") == "changelog":
        entries = ((data["changelogs"].get("products") or {}).get(source["product"]) or {}).get("entries") or []
        if not entries or entries[0]["date"] not in text:
            c.fail(f"{role}: newest changelog entry in the data "
                   f"({entries[0]['date'] if entries else 'none'}) is not in the newest column")


def check_blogs(checks: dict, role: str, dom, rid_target: dict, plan: dict, slide_cx: int) -> None:
    c, fit = checks["blogs"], checks["fit"]
    groups = sl.blog_groups(dom, slide_cx)
    shown = [sl.read_blog_entry(g["box"], rid_target) for g in groups]
    planned = plan["entries"][:len(groups)]
    if len(shown) != len(planned):
        c.fail(f"{role}: {len(shown)} entry boxes filled, {len(planned)} planned")
        return
    for got, want in zip(shown, planned):
        if got["date_display"] != want["date_display"] or got["url"] != want["url"]:
            c.fail(f"{role}: box shows '{got['date_display']}' -> {got['url']}, planned "
                   f"'{want['date_display']}' -> {want['url']}")
        full = norm(sl.strip_markup(want["title"]))
        title = norm(sl.strip_markup(got["title"]))
        if title != full and not (title.endswith("…") and full.startswith(title[:-1].rstrip())):
            c.fail(f"{role}: title '{title}' != planned '{full}'")
    urls = [e["url"] for e in shown]
    if len(set(urls)) != len(urls):
        c.fail(f"{role}: the same post is linked twice")
    dates = [e["date"] for e in planned]
    if dates != sorted(dates, reverse=True):
        c.fail(f"{role}: posts are not newest-first")
    one_line = min(sl.box(g["box"])[3] for g in groups)
    for g in groups:
        paras = px.paragraphs(g["box"])
        left, _t, right, _b = compose.insets(g["box"])
        width = (sl.box(g["box"])[2] - left - right) / EMU_PT
        segs = [(px.run_text(r), sl.is_bold(r)) for r in px.para_runs(paras[1])]
        lines = textfit.line_count(segs, compose.style_of(paras[1]).size_pt, width)
        holds = 1 + round((sl.box(g["box"])[3] - one_line) / 139700)
        if lines > holds:
            fit.fail(f"{role}: title '{norm(sl.para_text(paras[1]))[:50]}' needs {lines} lines, "
                     f"its box holds {holds}")


def check_webinars(checks: dict, role: str, dom, rid_target: dict, plan: dict, build_date: str) -> None:
    c, fit = checks["events"], checks["fit"]
    slots = sl.webinar_slots(dom)
    planned = plan["slots"][:len(slots)] if len(plan["slots"]) >= len(slots) else plan["slots"]
    if len(slots) != len(planned):
        c.fail(f"{role}: {len(slots)} cards on the slide, {len(planned)} planned")
        return
    for slot, want in zip(slots, planned):
        got = sl.read_slot(slot, rid_target)
        label = f"{role} / {want['title'][:40]}"
        if (got["date_label"], got["kind"], norm(got["title"]), got["url"]) != \
                (want["date_label"], want["kind"], norm(want["title"]), want["url"]):
            c.fail(f"{label}: card shows {got['date_label']} | {got['kind']} | {got['title']} -> {got['url']}")
        if not got["description"].strip():
            c.fail(f"{label}: card has no description")
        elif not any(norm(opt).startswith(norm(got["description"]).rstrip("…").rstrip())
                     for opt in want["description_options"]):
            c.fail(f"{label}: description is not taken from the planned options")
        if want["date"] < build_date:
            c.fail(f"{label}: event date {want['date']} is before the build date")
        _l, top, _r, _b = compose.insets(slot)
        room = (sl.box(slot)[3] - top - layout.CARD_ICON_ROW) / EMU_PT
        used = body_height_pt(slot)
        if used > room * FIT_TOLERANCE:
            fit.fail(f"{label}: text needs ~{used:.0f} pt, {room:.0f} pt are free above the icons")
        icons = [n for n in sl.slot_decorations(dom, slot) if not sl.is_flag(n)]
        if len(icons) != len(want["products"]):
            c.fail(f"{label}: {len(icons)} product icon(s) on the card, {len(want['products'])} planned")
        if sum(1 for n in sl.slot_decorations(dom, slot) if sl.is_flag(n)) != 1:
            c.fail(f"{label}: card should carry exactly one language flag")


def check_featured(checks: dict, role: str, doms: list, plan: dict, build_date: str) -> None:
    c = checks["events"]
    if len(doms) != len(plan["events"]):
        c.fail(f"{role}: {len(doms)} featured slides, {len(plan['events'])} events planned")
        return
    for (dom, rid_target), want in zip(doms, plan["events"]):
        got = sl.read_featured(dom, rid_target)
        if norm(got["title"]) != norm(want["title"]) or got["date_display"] != want["date_display"]:
            c.fail(f"{role}: slide shows '{got['title']}' on '{got['date_display']}', planned "
                   f"'{want['title']}' on '{want['date_display']}'")
        if got["links"] != want["links"]:
            c.fail(f"{role} / {want['title'][:40]}: links {got['links']} != planned {want['links']}")
        points = [norm(sl.strip_markup(p)) for p in got["description_points"]]
        if points != [norm(sl.strip_markup(p)) for p in want["description_points"]]:
            c.fail(f"{role} / {want['title'][:40]}: description differs from the plan")
        if want["date"] < build_date:
            c.fail(f"{role} / {want['title'][:40]}: event date {want['date']} is before the build date")


def check_cover(checks: dict, dom, plan: dict) -> None:
    c = checks["freshness"]
    text = norm(slide_text(dom))
    if plan["month_label"] not in text:
        c.fail(f"cover does not show the build month '{plan['month_label']}'")
    if "READY TO PUBLISH" in text.upper():
        c.fail("cover still claims 'READY TO PUBLISH' — an unreviewed build must not")
    for line in plan["status_lines"]:
        if norm(line) not in text:
            c.fail(f"cover is missing the status line '{line}'")


# --------------------------------------------------------------------------- whole-deck checks ----
def check_layout(c: Check, slides: list[Path], kinds: dict, slide_cx: int, slide_cy: int) -> None:
    for slide in slides:
        dom = minidom.parse(str(slide))
        role = deck.get_role(slide)
        problems = layout.bounds_problems(dom, slide_cx, slide_cy)
        kind = kinds.get(role)
        if kind == "columns":
            problems += layout.column_problems(sl.columns(dom),
                                               lambda p: compose.style_of(p, default_size=6.5))
        elif kind == "blogs":
            problems += layout.blog_problems(sl.blog_groups(dom, slide_cx), slide_cx)
        elif kind == "webinars":
            problems += layout.card_problems(dom)
        for problem in problems:
            c.fail(f"{role or slide.name}: {problem}")


def check_links(c: Check, slides: list[Path]) -> None:
    for slide in slides:
        dom = minidom.parse(str(slide))
        rels = minidom.parse(str(deck.rels_path(slide)))
        by_id = {r.getAttribute("Id"): r for r in rels.getElementsByTagName("Relationship")}
        for hl in dom.getElementsByTagName("a:hlinkClick"):
            rid = hl.getAttribute("r:id")
            if not rid:
                continue  # an action link (e.g. "next slide"), not a URL
            rel = by_id.get(rid)
            if rel is None:
                c.fail(f"{slide.name}: hyperlink {rid} has no relationship (dangling link)")
                continue
            if rel.getAttribute("TargetMode") != "External":
                continue
            url = rel.getAttribute("Target")
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname:
                c.fail(f"{slide.name}: link is not a web address: {url!r}")
            elif INTERNAL_HOST_RE.search(parsed.hostname):
                c.fail(f"{slide.name}: link points at an internal host: {url}")


def check_formatting(c: Check, slides: list[Path], template: Path, unpacked: Path) -> None:
    """A built slide may only use colours and typefaces the template already contains, and every font
    the deck embeds must be the font its name says (PowerPoint draws with the embedded data when the
    typeface is not installed, lib/fonts.py)."""
    for f in fonts.mismatched(unpacked):
        c.fail(f"embedded font '{f['typeface']}' ({f['slot']}) carries "
               f"{repr(f['family']) if f['family'] else 'no readable font'} data")
    known_hex, known_fonts = set(), set()
    with zipfile.ZipFile(template) as z:
        for name in z.namelist():
            if name.startswith("ppt/") and name.endswith(".xml"):
                text = z.read(name).decode("utf-8", "replace")
                known_hex |= {h.upper() for h in re.findall(r'srgbClr val="([0-9A-Fa-f]{6})"', text)}
                known_fonts |= set(re.findall(r'typeface="([^"]+)"', text))
    for slide in slides:
        text = slide.read_text(encoding="utf-8")
        if "**" in slide_text(minidom.parseString(text)):
            c.fail(f"{slide.name}: shows a literal '**' (bold markup that never became bold)")
        new_hex = {h.upper() for h in re.findall(r'srgbClr val="([0-9A-Fa-f]{6})"', text)} - known_hex
        new_fonts = set(re.findall(r'typeface="([^"]+)"', text)) - known_fonts
        if new_hex or new_fonts:
            c.fail(f"{slide.name}: formatting not in the template — colours {sorted(new_hex)}, "
                   f"fonts {sorted(new_fonts)}")


def check_disclosure(c: Check, slides: list[Path], data: dict, unpacked: Path) -> None:
    for part in ("ppt/authors.xml", "ppt/commentAuthors.xml", "ppt/comments"):
        if (unpacked / part).exists():
            c.fail(f"{part}: the deck carries the people who commented on the reference deck")
    public = {x.upper() for x in CVE_RE.findall(json.dumps(data["releases"]) + json.dumps(data["changelogs"]))}
    for slide in slides:
        cited = {x.upper() for x in CVE_RE.findall(slide_text(minidom.parse(str(slide))))}
        if cited - public:
            c.fail(f"{slide.name}: cites {sorted(cited - public)}, which the fetched public release "
                   "notes do not name")


# --------------------------------------------------------------------------- driver ---------------
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("deck", nargs="?", default=None)
    ap.add_argument("--plan", default=str(DATA_DIR / "deck-plan.json"))
    ap.add_argument("--report", default=str(DATA_DIR / "verify-report.json"))
    args = ap.parse_args()

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    deck_path = Path(args.deck) if args.deck else Path(read_json("build-report.json").get("deck", ""))
    if not deck_path.is_file():
        log(f"ERROR: deck not found: {deck_path}")
        return 1
    cfg = load_config("deck.yaml")
    rules = {r["role"]: r for r in cfg["slides"]}
    data = {name: read_json(f"{name}.json") for name in ("releases", "changelogs", "blogs", "events")}
    build_date = plan["build"]["date"]
    names = ("package", "columns", "blogs", "events", "fit", "layout", "links", "disclosure", "formatting",
             "freshness")
    checks = {n: Check(n) for n in names}

    tmp = Path(tempfile.mkdtemp(prefix="verify-"))
    try:
        try:
            px.unpack(deck_path, tmp)
            ordered = deck.ordered_slides(tmp)
        except Exception as exc:  # noqa: BLE001
            checks["package"].fail(f"deck cannot be opened: {exc}")
            ordered = []
        roles_in_deck = [deck.get_role(s) for s in ordered]
        expected: list[str] = []
        for sp in plan["slides"]:
            expected += [sp["role"]] * (len(sp["events"]) if sp["kind"] == "featured" else 1)
        if roles_in_deck != expected:
            checks["package"].fail(f"slide roles {roles_in_deck} != planned {expected}")
        for slide in ordered:
            if re.search(r'<p:sld\b[^>]*\bshow="0"', slide.read_text(encoding="utf-8")[:3000]):
                checks["package"].fail(f"{slide.name} is hidden")
        pres = minidom.parse(str(tmp / "ppt" / "presentation.xml")) if ordered else None
        slide_cx = int(pres.getElementsByTagName("p:sldSz")[0].getAttribute("cx")) if pres else 0
        slide_cy = int(pres.getElementsByTagName("p:sldSz")[0].getAttribute("cy")) if pres else 0

        by_role: dict[str, list[Path]] = {}
        for slide, role in zip(ordered, roles_in_deck):
            by_role.setdefault(role, []).append(slide)
        if roles_in_deck == expected:
            for sp in plan["slides"]:
                role, kind = sp["role"], sp["kind"]
                loaded = [(minidom.parse(str(s)), px.load_rels(deck.rels_path(s))[1]) for s in by_role[role]]
                dom, rid_target = loaded[0]
                if kind == "columns":
                    check_columns(checks, role, dom, rid_target, sp, rules[role], data)
                elif kind == "blogs":
                    check_blogs(checks, role, dom, rid_target, sp, slide_cx)
                elif kind == "webinars":
                    check_webinars(checks, role, dom, rid_target, sp, build_date)
                elif kind == "featured":
                    check_featured(checks, role, loaded, sp, build_date)
                elif kind == "cover":
                    check_cover(checks, dom, sp)
        if ordered:
            check_layout(checks["layout"], ordered, {sp["role"]: sp["kind"] for sp in plan["slides"]},
                         slide_cx, slide_cy)
            check_links(checks["links"], ordered)
            check_formatting(checks["formatting"], ordered, REPO_ROOT / cfg["deck"]["template"], tmp)
            check_disclosure(checks["disclosure"], ordered, data, tmp)
        month = f"{MONTH_NAMES[int(build_date[5:7]) - 1]} {build_date[:4]}"
        if not any(sp["kind"] == "cover" for sp in plan["slides"]):
            checks["freshness"].info.append(f"no cover slide in this deck (build month {month})")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    ok = all(c.ok for c in checks.values())
    result = {"deck": str(deck_path), "build_date": build_date, "ok": ok,
              "build_status": plan["build"]["status"], "omitted": plan["omitted"],
              "checks": {n: {"ok": c.ok, "notes": c.notes, "info": c.info} for n, c in checks.items()}}
    Path(args.report).write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(f"\n=== verify_build: {deck_path.name} ===")
    for name, c in checks.items():
        print(f"[{'PASS' if c.ok else 'FAIL'}] {name}")
        for note in c.notes:
            print(f"        - {note}")
        for note in c.info:
            print(f"        ({note})")
    for item in plan["omitted"]:
        print(f"        (left out: {item['role']} — {item['reason']})")
    print(f"\n=== OVERALL: {'PASS' if ok else 'FAIL'}"
          + (" (degraded: an optional source was unavailable)" if ok and plan["build"]["status"] != "ok" else "")
          + " ===")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
