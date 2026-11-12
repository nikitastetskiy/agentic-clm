#!/usr/bin/env python3
"""Turn the fetched data into the CONTENT PLAN of the weekly deck (data/deck-plan.json).

The plan says, slide by slide (config/deck.yaml order), exactly what the deck will show: which three
months each release slide covers and every entry in them, the eight blog posts, the event cards, the
featured events. It is a pure function of data/*.json, state/enrichment.json and the config — no
network, no template — so a deck can be explained (and a wrong one debugged) by reading one file.

Wording comes from, in order of preference:
  1. state/enrichment.json — text a person wrote (seeded from the reference deck, or edited by hand)
     or that the enrichment step wrote once and cached;
  2. the public source itself, condensed by fixed rules (first sentence of the lead note, a category
     label such as "Bug fixes", an event page's abstract).
Entries carry both as `options` — the rich wording first, then a short form of it, a release's category
label last — and the builder uses the richest one that fits the box.

Disclosure guard: a CVE id may only appear in the deck if the public release notes for that same
release name it. Wording that cites any other CVE is dropped and reported, whoever wrote it.

Exit 0 with ``build.status`` "ok" or "degraded" (an optional source was unavailable and its slides
are left out); exit 1 if a required source is missing or empty.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import state  # noqa: E402
from lib.common import DATA_DIR, MONTH_NAMES, keeps_words, load_config, log, read_json, today  # noqa: E402
from lib.event_media import detect_language  # noqa: E402
from lib.regions import event_region  # noqa: E402

CVE_RE = re.compile(r"CVE-\d{4}-\d{4,7}", re.I)
DATED_ALPHA_RE = re.compile(r"^(\d+\.\d+\.\d+)-alpha\d{8}$")
SENTENCE_RE = re.compile(r".+?(?:[.!?](?=\s|$)|$)")
# Where a sentence can stop early and still say the same thing: before a clause that only adds to it
# ("…, allowing X", "…, which Y"). Not before a condition ("…, when Z"), whose loss would change it.
CLAUSE_RE = re.compile(r",\s+(?=(?:with|so|which|including|(?:\w+ly\s+)?(?:allowing|enabling|ensuring|preventing|"
                       r"making|giving|letting|providing|reducing|improving|helping|removing|simplifying|avoiding|"
                       r"keeping|preserving|resolving|eliminating))\b)")
MIN_CLAUSE = 40   # what is left of a shortened sentence is at least this long
REPEATED_LABEL_RE = re.compile(r"\*\*([^*]+):\*\*\s+\1\b", re.I)
# Section headings of the various changelog / release-notes formats, by what they mean.
SECTION_KINDS = {
    "features": ("new features", "features", "highlights"),
    "improvements": ("enhancements", "improvements", "improvements and behavior changes"),
    "fixes": ("bug fixes", "bugfixes", "bug fixes and security patches"),
    "security": ("security",),
}
LABEL_WORDS = {"features": "new features", "improvements": "enhancements", "fixes": "bug fixes",
               "security": "security updates"}
# Notes that say nothing specific; never used as a release's lead sentence.
BOILERPLATE_RE = re.compile(r"security vulnerabilities have been addressed and resolved", re.I)
DECK_PRODUCTS = ("terraform", "vault")   # products that have an icon on the event cards
GENERIC_EVENT_LINE = {
    "WORKSHOP": "Join this hands-on session with HashiCorp experts for a guided, practical walkthrough.",
    "WEBINAR": "Join this HashiCorp session to see what is new and how to put it to work in your environment.",
}
SHORT_EVENT_LINE = "Join this HashiCorp session."   # fits one line of any card: a description is never cut


# --------------------------------------------------------------------------- small helpers --------
def first_sentences(text: str, max_chars: int, max_sentences: int = 1) -> str:
    """The first sentence(s) of `text`, made shorter where they pass `max_chars`: fewer sentences, then
    the first one without its closing clauses ("…, allowing X"). Wording is never cut at a word with an
    ellipsis: a sentence that cannot be shortened stays whole, and the builder leaves out an entry
    before it runs out of room."""
    text = " ".join((text or "").split())
    sentences = [s.strip() for s in SENTENCE_RE.findall(text)[:max_sentences]]
    while len(sentences) > 1 and len(" ".join(sentences)) > max_chars:
        sentences.pop()
    out = " ".join(sentences).strip() or text
    if len(out) > max_chars:
        ends = [m.start() for m in CLAUSE_RE.finditer(out) if MIN_CLAUSE <= m.start() < max_chars]
        ends = [end for end in ends if out[:end].count("**") % 2 == 0]   # never inside a bold phrase
        if ends:
            out = out[:ends[-1]] + "."
    return out if out.count("**") % 2 == 0 else text


def clean_markup(text: str) -> str:
    """Wording as stored, minus its slips: a doubled full stop, a label the text then says again
    ("**Vault 2.0.4:** Vault 2.0.4 is now available" reads "**Vault 2.0.4** is now available")."""
    text = re.sub(r"\.\.(?!\.)", ".", " ".join((text or "").split()))
    return REPEATED_LABEL_RE.sub(r"**\1**", text)


def labelled(label: str | None, text: str) -> str:
    """`text` under its bold label, which is not said twice where the text opens with it."""
    return clean_markup(f"**{label}:** {text}") if label else text


def join_words(words: list[str]) -> str:
    if len(words) <= 1:
        return "".join(words)
    return ", ".join(words[:-1]) + " and " + words[-1]


def section_kinds(sections: dict) -> list[str]:
    names = {name.strip().lower() for name, items in (sections or {}).items() if items}
    return [kind for kind, aliases in SECTION_KINDS.items() if names & set(aliases)]


def section_label(sections: dict) -> str:
    """A category label for a release: "Bug fixes", "New features, enhancements and bug fixes"."""
    named = [name.strip() for name, items in (sections or {}).items() if items]
    if len(named) == 1 and len(named[0]) <= 40:
        # one section only: its own heading says it best ("Bug fixes and security patches")
        return named[0][0].upper() + named[0][1:].lower()
    kinds = section_kinds(sections)
    if not kinds:
        return "Maintenance release"
    label = join_words([LABEL_WORDS[k] for k in kinds])
    return label[0].upper() + label[1:]


def lead_sentence(sections: dict, max_chars: int = 210) -> str | None:
    """What a release ADDS, as one sentence of markup: its first feature, else its first improvement.
    None for a release that only fixes things — quoting one arbitrary fix would misrepresent a
    maintenance or security release, so those are described by their category label instead."""
    by_name = {name.strip().lower(): items for name, items in (sections or {}).items()}
    for kind in ("features", "improvements"):
        for alias in SECTION_KINDS[kind]:
            for item in by_name.get(alias, []):
                if BOILERPLATE_RE.search(item.get("text", "")):
                    continue
                text = first_sentences(item["text"], max_chars)
                return labelled(item.get("lead"), text)
    return None


def plain_size(bullets: list[str]) -> int:
    return sum(len(b.replace("**", "")) for b in bullets)


def public_text(*entries: dict) -> str:
    """Everything the public source says about the given release / changelog entries."""
    chunks = []
    for entry in entries:
        for items in (entry.get("sections") or {}).values():
            chunks += [f"{it.get('lead', '')} {it.get('text', '')}" for it in items]
        chunks += [f"{it.get('title', '')} {it.get('text', '')}" for it in entry.get("items") or []]
    return " ".join(chunks)


class Planner:
    def __init__(self, deck_cfg: dict, data: dict, enrich: dict, run_day: date):
        self.cfg = deck_cfg
        self.data = data
        self.enrich = enrich
        self.day = run_day
        self.notes: list[str] = []      # things a reviewer should know
        self.problems: list[str] = []   # reasons the plan cannot be built
        self.omitted: list[dict] = []

    # ----------------------------------------------------------------------- wording --------------
    def curated(self, key: str, *sources: dict, table: str = "highlights") -> list[str] | None:
        """Stored wording for `key` (its short form with ``table="shorts"``), unless it fails the
        disclosure guard."""
        hit = (self.enrich.get(table) or {}).get(key)
        bullets = [clean_markup(b) for b in (hit or {}).get("bullets", []) if b and b.strip()]
        if not bullets:
            return None
        known = {c.upper() for c in CVE_RE.findall(public_text(*sources))}
        cited = {c.upper() for b in bullets for c in CVE_RE.findall(b)}
        if cited - known:
            self.notes.append(
                f"{key}: stored wording ({hit.get('source', '?')}) cites {sorted(cited - known)}, which the "
                "public release notes for that release do not name — dropped (disclosure guard)")
            return None
        return bullets

    def short_option(self, key: str, rich: list[str], openings: list[str], *sources: dict) -> list[str]:
        """What an entry shows where its month has no room for `rich`: the short form the wording step
        stored, else the shortest of `openings` (opening sentences of its wording, without their closing
        clauses). Never longer than `rich`: a short option that is would help no column."""
        short = self.curated(key, *sources, table="shorts")
        if short and plain_size(short) < plain_size(rich):
            return short
        return [min(openings, key=lambda s: plain_size([s]))]

    # ----------------------------------------------------------------------- columns --------------
    def month_columns(self, entries: list[dict]) -> list[tuple[str, list[dict]]]:
        """The newest N months that have entries, oldest first: [(YYYY-MM, entries newest first)]."""
        months: dict[str, list[dict]] = {}
        for entry in sorted(entries, key=lambda e: (e["date"], e.get("version", "")), reverse=True):
            months.setdefault(entry["date"][:7], []).append(entry)
        keep = sorted(months)[-int(self.cfg["deck"].get("columns", 3)):]
        return [(m, months[m]) for m in keep]

    def release_entry(self, product: str, rel: dict, style: dict) -> dict:
        m = DATED_ALPHA_RE.match(rel["version"])
        shown = f"v{m.group(1)}-alpha" if m else f"v{rel['version']}"
        heading = [{"text": f"{style.get('heading_prefix', '')}{shown} ({rel['date']})", "url": rel["url"]}]
        if rel.get("is_prerelease"):
            options = [[style.get("prerelease_label", "Pre-release")]]
        else:
            options = self.notes_options(f"{product}/{rel['version']}", rel, style)
        return {"key": f"{product}/{rel['version']}", "date": rel["date"], "heading": heading,
                "options": options}

    def notes_options(self, key: str, rel: dict, style: dict) -> list[list[str]]:
        """[rich, short, terse]: stored wording (else what the release adds, as a sentence), its short
        form, then a category label. A slide whose style says ``wording: label`` only ever gets the label."""
        sections = rel.get("sections") or {}
        if style.get("wording") == "label":
            return [[section_label(sections)]]
        stored = self.curated(key, rel)
        lead = lead_sentence(sections)
        rich = stored or ([lead] if lead else None)
        return self.worded(key, rich, [section_label(sections)], rel)

    def worded(self, key: str, rich: list[str] | None, terse: list[str], *sources: dict) -> list[list[str]]:
        """A release entry's options. Wording that is short already is its own short form, so that a
        month with little room keeps it while a longer neighbour gives way to its short form."""
        if not rich:
            return [terse]
        return [rich, self.short_option(key, rich, [first_sentences(rich[0], 150)], *sources), terse]

    def vault_day_entry(self, product: str, group: list[dict], style: dict) -> dict:
        """One entry for every Vault version released on the same day: the newest line first, then
        the Enterprise LTS patches, each version linked to its own release notes."""
        group = sorted(group, key=lambda r: [int(x) for x in r["version"].split(".")], reverse=True)
        lead = next((r for r in group if not r.get("enterprise_only")), None)
        heading = []
        for i, rel in enumerate(group):
            if i:
                heading.append({"text": " / "})
            heading.append({"text": f"v{rel['version']}", "url": rel["url"]})
        heading.append({"text": f" ({group[0]['date']})"})
        if lead is None:
            key = f"{product}/{group[0]['version']}"
            options = self.worded(key, self.curated(key, *group),
                                  [style.get("lts_label", "Bug fixes and improvements")], *group)
        else:
            key = f"{product}/{lead['version']}"
            options = self.notes_options(key, lead, style)
        return {"key": key, "date": group[0]["date"], "heading": heading, "options": options}

    def plan_release_columns(self, rule: dict) -> dict | None:
        product = rule["source"]["product"]
        style = rule.get("style") or {}
        rel_data = self.data["releases"]
        node = (rel_data.get("terraform_enterprise") if product == "terraform-enterprise"
                else (rel_data.get("products") or {}).get(product)) or {}
        releases = node.get("releases") or []
        if not releases:
            self.problems.append(f"{rule['role']}: no release data for {product}")
            return None
        columns = []
        for month, rels in self.month_columns(releases):
            if style.get("group_same_day"):
                days: dict[str, list[dict]] = {}
                for rel in rels:
                    days.setdefault(rel["date"], []).append(rel)
                entries = [self.vault_day_entry(product, days[d], style) for d in sorted(days, reverse=True)]
            else:
                entries = [self.release_entry(product, rel, style) for rel in rels]
            columns.append({"month": month, "header": MONTH_NAMES[int(month[5:7]) - 1], "entries": entries})
        return {"columns": columns}

    def plan_changelog_columns(self, rule: dict) -> dict | None:
        product = rule["source"]["product"]
        node = (self.data["changelogs"].get("products") or {}).get(product) or {}
        if node.get("status") != "ok" or not node.get("entries"):
            self.problems.append(f"{rule['role']}: no changelog data for {product}")
            return None
        columns = []
        for month, entries in self.month_columns(node["entries"]):
            planned = []
            for entry in entries:
                key = f"{product}/{entry['date']}"
                items = entry["items"]
                options = []
                stored = self.curated(key, entry)
                if stored:
                    options.append(stored)
                elif any(it.get("title") for it in items):
                    options.append([labelled(it.get("title"), first_sentences(it["text"], 260, 2))
                                    for it in items[:3]])
                else:
                    more = f" (+{len(items) - 1} more updates)" if len(items) > 1 else ""
                    options.append([first_sentences(items[0]["text"], 300, 2) + more])
                # The short option's openings: the stored wording's opening sentence and the source's.
                # Either can run past the other.
                openings = [first_sentences(stored[0], 150)] if stored else []
                openings.append(labelled(items[0].get("title"), first_sentences(items[0]["text"], 150)))
                options.append(self.short_option(key, options[0], openings, entry))
                planned.append({"key": key, "date": entry["date"],
                                "heading": [{"text": entry["date"], "url": entry["url"]}],
                                "options": options})
            columns.append({"month": month, "header": MONTH_NAMES[int(month[5:7]) - 1], "entries": planned})
        return {"columns": columns}

    # ----------------------------------------------------------------------- blogs ----------------
    def plan_blogs(self, rule: dict) -> dict | None:
        product = rule["source"]["product"]
        posts = ((self.data["blogs"].get("products") or {}).get(product) or {}).get("recent") or []
        if not posts:
            self.problems.append(f"{rule['role']}: no blog posts for {product}")
            return None
        emphasised = self.enrich.get("blog_titles") or {}
        shorts = self.enrich.get("shorts") or {}
        entries = []
        for post in posts:
            d = date.fromisoformat(post["date"])
            markup = (emphasised.get(state.norm_url(post["url"])) or {}).get("title_markup", "")
            # Emphasis is only reused when it decorates the post's exact title.
            title = markup if " ".join(markup.replace("**", "").split()) == " ".join(post["title"].split()) \
                else post["title"]
            entries.append({"date": post["date"], "date_display": f"{MONTH_NAMES[d.month - 1]} {d.day}",
                            "title": title, "url": post["url"]})
            # A stored shorter title, for a box the title does not fit: reused only while it is still the
            # post's title with words left out.
            short = " ".join(((shorts.get(state.norm_url(post["url"])) or {}).get("bullets") or [""])[0].split())
            if short and keeps_words(short, post["title"]) and len(short.replace("**", "")) < len(post["title"]):
                entries[-1]["title_short"] = short
        return {"entries": entries}

    # ----------------------------------------------------------------------- events ---------------
    def event_slot(self, ev: dict) -> dict:
        d = date.fromisoformat(ev["date"])
        kind = (ev.get("type") or "WEBINAR").upper()
        stored = ((self.enrich.get("events") or {}).get(state.event_key(ev["url"])) or {}).get("description")
        options = [o for o in (stored, ev.get("description"), ev.get("abstract")) if o and o.strip()]
        options += [GENERIC_EVENT_LINE.get(kind, GENERIC_EVENT_LINE["WEBINAR"]), SHORT_EVENT_LINE]
        language = ev.get("language") or detect_language(ev["title"])
        if language == "en" and event_region(ev) == "AMERICAS":
            language = "us"
        return {"date": ev["date"], "date_label": f"{d.strftime('%b').upper()} {d.day}, {d.year}",
                "kind": kind, "title": ev["title"], "url": ev["url"],
                # a card's description is plain text: stored wording may mark bold, which it cannot show
                "description_options": [" ".join(o.replace("**", "").split()) for o in options],
                "products": [p for p in DECK_PRODUCTS if p in (ev.get("products") or [])],
                "language": language}

    def plan_webinars(self, rule: dict, used_other: set) -> dict | None:
        events = self.data["events"]
        product = rule["source"]["product"]
        if events.get("status") != "ok":
            self.omitted.append({"role": rule["role"], "reason": "events source unavailable"})
            return None
        slots = int(rule.get("slots", 3))
        fill = min(slots, int(rule.get("fill", slots)))
        webinars = events.get("webinars") or {}
        every = sorted(webinars.get(product) or [], key=lambda e: e["date"])
        own, later = every[:slots], every[slots:]
        # Up to `fill` slots are backfilled with sessions that belong to no deck product; each of those
        # is shown on one slide only. A session badged for both products appears on both slides.
        for ev in sorted(webinars.get("other") or [], key=lambda e: e["date"]):
            ident = (ev["title"].strip().lower(), ev["date"])
            if len(own) >= fill:
                break
            if ident not in used_other:
                used_other.add(ident)
                own.append(ev)
        if not own:
            self.omitted.append({"role": rule["role"], "reason": f"no upcoming {product} events"})
            return None
        return {"slots": [self.event_slot(ev) for ev in sorted(own, key=lambda e: e["date"])],
                "later": [f"{ev['date']} {ev['title']}" for ev in later]}

    def plan_featured(self, rule: dict) -> dict | None:
        featured = sorted((self.data["events"].get("featured") or []), key=lambda e: e["date"])
        if not featured:
            self.omitted.append({"role": rule["role"], "reason": "no upcoming featured events"})
            return None
        planned = []
        for ev in featured:
            points = [clean_markup(p) for p in ev.get("description_points") or [] if p.strip()]
            if not points:
                source = ev.get("description") or ev.get("abstract") or ""
                points = [s.strip() for s in SENTENCE_RE.findall(" ".join(source.split()))[:3] if s.strip()]
            if not points:
                where = f" in {ev['location']}" if ev.get("location") else ""
                points = [f"Join the HashiCorp and IBM team and industry peers at this in-person event{where}.",
                          "See the latest in infrastructure and security automation and meet our experts in person."]
            links = ev.get("links") or [{"label": "Register Here", "url": ev["url"]}]
            planned.append({"date": ev["date"], "date_display": ev["display_date"], "title": ev["title"],
                            "location": ev.get("location", ""), "links": links,
                            "description_points": points,
                            "products": [p for p in DECK_PRODUCTS if p in (ev.get("products") or [])],
                            "visuals": ev.get("visuals", "")})
        return {"events": planned}

    # ----------------------------------------------------------------------- cover ----------------
    def plan_cover(self) -> dict:
        d = self.day
        return {"month_label": f"{MONTH_NAMES[d.month - 1]} {d.year}",
                "status_lines": [self.cfg["deck"].get("cover_status", "AUTO-GENERATED"),
                                 f"Built – {d.day} {MONTH_NAMES[d.month - 1]} {d.year}"],
                "fiscal_year": f"FY{d.year % 100:02d}", "week": f"WEEK {d.isocalendar().week}"}

    # ----------------------------------------------------------------------- driver ---------------
    def build(self) -> dict:
        slides = []
        used_other: set = set()
        for rule in self.cfg["slides"]:
            kind = rule["kind"]
            source = (rule.get("source") or {}).get("type")
            if kind == "cover":
                body = self.plan_cover()
            elif kind == "static":
                body = {}
            elif kind == "columns" and source == "releases":
                body = self.plan_release_columns(rule)
            elif kind == "columns" and source == "changelog":
                body = self.plan_changelog_columns(rule)
            elif kind == "blogs":
                body = self.plan_blogs(rule)
            elif kind == "webinars":
                body = self.plan_webinars(rule, used_other)
            elif kind == "featured":
                body = self.plan_featured(rule)
            else:
                self.problems.append(f"{rule['role']}: unknown slide kind/source {kind}/{source}")
                body = None
            if body is not None:
                slides.append({"role": rule["role"], "kind": kind, **body})
        shown = {s["role"] for s in slides}
        for rule in self.cfg["slides"]:
            if rule.get("only_with") and rule["role"] in shown and rule["only_with"] not in shown:
                slides = [s for s in slides if s["role"] != rule["role"]]
                self.omitted.append({"role": rule["role"], "reason": f"no {rule['only_with']} slide"})
        events_status = self.data["events"].get("status", "missing")
        if events_status != "ok":
            self.notes.append(f"events source: {events_status} "
                              f"({self.data['events'].get('note', 'no detail')}) — event slides left out")
        elif self.data["events"].get("note"):
            self.notes.append(f"events: {self.data['events']['note']}")
        if self.data["blogs"].get("note"):
            self.notes.append(f"blogs: {self.data['blogs']['note']}")
        status = "failed" if self.problems else ("degraded" if events_status != "ok" else "ok")
        return {"build": {"date": self.day.isoformat(), "status": status, "notes": self.notes,
                          "problems": self.problems,
                          "sources": {name: self.data[name].get("generated_at")
                                      for name in ("releases", "changelogs", "blogs", "events")}},
                "slides": slides, "omitted": self.omitted}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(DATA_DIR / "deck-plan.json"))
    args = ap.parse_args()

    data = {name: read_json(f"{name}.json") for name in ("releases", "changelogs", "blogs", "events")}
    for name in ("releases", "changelogs", "blogs"):
        if not data[name]:
            log(f"ERROR: data/{name}.json is missing — run the fetchers first")
            return 1
    data["events"] = data["events"] or {"status": "missing", "note": "data/events.json not found"}
    plan = Planner(load_config("deck.yaml"), data, state.load("enrichment.json"), today()).build()
    Path(args.out).write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    for slide in plan["slides"]:
        if slide["kind"] == "columns":
            detail = " | ".join(f"{c['header']}: {len(c['entries'])}" for c in slide["columns"])
        elif slide["kind"] == "blogs":
            detail = f"{len(slide['entries'])} posts, newest {slide['entries'][0]['date']}"
        elif slide["kind"] == "webinars":
            detail = "; ".join(f"{s['date_label']} {s['kind']}" for s in slide["slots"])
        elif slide["kind"] == "featured":
            detail = "; ".join(f"{e['date_display']}: {e['title']}" for e in slide["events"])
        else:
            detail = slide.get("month_label", "")
        log(f"  {slide['role']:<32} {detail}")
    for item in plan["omitted"]:
        log(f"  {item['role']:<32} OMITTED — {item['reason']}")
    for note in plan["build"]["notes"]:
        log(f"  note: {note}")
    for problem in plan["build"]["problems"]:
        log(f"  PROBLEM: {problem}")
    log(f"plan [{plan['build']['status']}] -> {args.out}")
    return 1 if plan["build"]["status"] == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
