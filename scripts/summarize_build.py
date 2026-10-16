#!/usr/bin/env python3
"""Summarise a weekly build for people (and record a failed one for the repair agent).

    python scripts/summarize_build.py                       # markdown summary of the last build
    python scripts/summarize_build.py --failure STEP CODE LOG   # write output/failure.json

The summary is what a reviewer needs before sharing the deck: what each slide shows, what was left
out and why, what the enrichment step wrote this week, and the gate's verdict. It is printed, saved
as output/summary-<date>.md, and used as the description of the published release.

The failure record (step, exit code, the tail of that step's log) is the starting point the
self-heal pipeline hands to the repair agent.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.common import REPO_ROOT, read_json  # noqa: E402

OUTPUT_DIR = REPO_ROOT / "output"
LOG_TAIL_LINES = 120


def record_failure(step: str, code: int, log_file: str) -> int:
    path = Path(log_file)
    tail = path.read_text(encoding="utf-8", errors="replace").splitlines()[-LOG_TAIL_LINES:] if path.exists() else []
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "failure.json").write_text(json.dumps({
        "step": step, "exit_code": code, "log": str(path),
        "failed_at": datetime.now(timezone.utc).isoformat(), "log_tail": tail,
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return 0


def slide_line(slide: dict) -> str:
    kind = slide["kind"]
    if kind == "columns":
        cols = "; ".join(f"{c['header']}: {len(c['entries'])}" + (f" (+{c['trimmed']} trimmed)" if c["trimmed"] else "")
                         for c in slide["columns"])
        newest = slide["columns"][-1]["entries"][0]["heading"] if slide["columns"][-1]["entries"] else "-"
        return f"{cols} — newest: {newest}"
    if kind == "blogs":
        first = slide["entries"][0]
        return f"{len(slide['entries'])} posts — newest: {first['date_display']}, {first['title'][:60]}"
    if kind == "webinars":
        shown = "; ".join(f"{e['date_label']} {e['kind'].title()}" for e in slide["events"])
        more = f" (+{len(slide['not_shown'])} later ones not shown)" if slide["not_shown"] else ""
        return shown + more
    if kind == "featured":
        return "; ".join(f"{e['date_display']}: {e['title']}" for e in slide["events"])
    if kind == "cover":
        return slide.get("month_label", "")
    return ""


def summary() -> str:
    build = read_json("build-report.json")
    verify = read_json("verify-report.json")
    enrich = read_json("enrich-report.json")
    if not build:
        return "No build report found (data/build-report.json) — the build did not get that far.\n"
    plan = build["build"]
    deck = Path(build["deck"])
    verdict = "PASS" if verify.get("ok") else "FAIL" if verify else "not run"
    lines = [f"# HashiCorp product updates deck — {plan['date']}", "",
             f"- **Deck:** `{deck.name}`"
             + (f" ({deck.stat().st_size / 1e6:.1f} MB)" if deck.exists() else " (file missing)"),
             f"- **Verification gate:** {verdict}",
             f"- **Sources:** {plan['status']}"
             + ("" if plan["status"] == "ok" else " — an optional source was unavailable, see below"),
             "- **Status:** generated automatically; review before sharing with a customer.", "",
             "| # | Slide | Content |", "|---|-------|---------|"]
    for slide in build["slides"]:
        pos = slide["positions"]
        where = str(pos[0]) if len(pos) == 1 else f"{pos[0]}–{pos[-1]}"
        lines.append(f"| {where} | {slide['role']} | {slide_line(slide)} |")
    if build["omitted"]:
        lines += ["", "**Left out of this deck**", ""]
        lines += [f"- {o['role']}: {o['reason']}" for o in build["omitted"]]
    if plan["notes"]:
        lines += ["", "**Notes**", ""] + [f"- {n}" for n in plan["notes"]]
    unscheduled = read_json("events.json").get("unscheduled") or []
    groups = (("online", "Online sessions promoted without a date", "a card on the webinar slide"),
              ("in person", "In-person events promoted without a date", "a Featured / In Person slide"),
              ("format not stated", "Promoted without a date or a stated format", "a card or a slide"))
    for fmt, heading, becomes in groups:
        items = [u for u in unscheduled if u.get("format", "format not stated") == fmt]
        if items:
            lines += ["", f"**{heading}** — not in the deck; with a date in `config/featured-events.yaml` "
                      f"each becomes {becomes}", ""]
            lines += [f"- {u['type'].title()}: [{u['title']}]({u['url']}) — {u['when']}"
                      + (f" ({', '.join(p.title() for p in u['products'])})" if u.get("products") else "")
                      for u in items]
    if enrich.get("accepted") or enrich.get("rejected"):
        lines += ["", f"**Wording written this run** ({enrich.get('model', '?')})", ""]
        lines += [f"- `{a['id']}`: {a['bullets'][0]}" for a in enrich.get("accepted", [])]
        lines += [f"- `{r['id']}`: rejected ({'; '.join(r['why'])}) — rule-based wording used"
                  for r in enrich.get("rejected", [])]
    failed = {n: c for n, c in (verify.get("checks") or {}).items() if not c["ok"]}
    if failed:
        lines += ["", "**Gate failures**", ""]
        for name, check in failed.items():
            lines += [f"- {name}: {note}" for note in check["notes"]]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--failure", nargs=3, metavar=("STEP", "CODE", "LOG"))
    args = ap.parse_args()
    if args.failure:
        return record_failure(args.failure[0], int(args.failure[1]), args.failure[2])
    text = summary()
    build = read_json("build-report.json")
    if build:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUTPUT_DIR / f"summary-{build['build']['date']}.md").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
