#!/usr/bin/env python3
"""Have the enrichment agent word what nobody has worded yet, once, and keep it.

The plan (plan_deck.py) fills every slide from fixed rules. For a NEW release with features, or a
changelog day with several changes, a rule can only quote the first note; a person would write one
sentence that sums the release up. This step asks the `deck-enrich` agent (opencode, a small model)
for that sentence and stores it in state/enrichment.json, so:

  * each item is worded ONCE — later builds reuse the stored text instead of rewording the deck
    every week (and a person can correct it by editing the state file);
  * the build itself stays deterministic: plan + build + verify never call a model.

The agent is given only public text (release notes, changelog entries, event abstracts) in a scratch
directory with no shell, no network and no view of the repository, and its answer is treated as
untrusted data: a bullet is accepted only if it is short enough, plainly worded, and every version
number, figure and CVE id in it occurs in the source it was given. Anything else is dropped and the
deck falls back to the rule-based wording.

Best-effort by design: no agent, no credentials, a timeout or a bad answer never fails a build.
Run AFTER plan_deck.py (it reads the plan to see what is on the slides) and re-plan afterwards.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import agent, state  # noqa: E402
from lib.common import DATA_DIR, load_config, log, read_json, today  # noqa: E402

DEFAULT_MODEL = "ica-gateway/claude-haiku-5-5"
MAX_CHARS = {"release": 230, "changelog": 260, "event": 330}
EVENT_MIN_ABSTRACT = 300   # shorter abstracts fit a card as they are
MIN_CHARS = 25
BANNED = ("leverage", "seamless", "robust", "delve", "cutting-edge", "game-chang", "revolution",
          "unlock", "empower", "in today's")
CVE_RE = re.compile(r"CVE-\d{4}-\d{4,7}", re.I)
VERSION_RE = re.compile(r"\b\d+\.\d+(?:\.\d+)?\b")
NUMBER_RE = re.compile(r"\b\d{2,}\b")
# Sections that say what a release adds; a release with none of them is described by a label instead.
ADDS = ("new features", "features", "highlights", "enhancements", "improvements",
        "improvements and behavior changes")


def source_index(data: dict) -> dict[str, dict]:
    """key -> the fetched record behind a plan entry ("vault/2.1.0", "hcp-terraform/2026-10-02")."""
    index: dict[str, dict] = {}
    rel = data["releases"]
    for product, node in (rel.get("products") or {}).items():
        for r in node.get("releases") or []:
            index[f"{product}/{r['version']}"] = {**r, "_display": node.get("display", product)}
    tfe = rel.get("terraform_enterprise") or {}
    for r in tfe.get("releases") or []:
        index[f"terraform-enterprise/{r['version']}"] = {**r, "_display": tfe.get("display", "Terraform Enterprise")}
    for product, node in (data["changelogs"].get("products") or {}).items():
        for e in node.get("entries") or []:
            index[f"{product}/{e['date']}"] = {**e, "_display": node.get("display", product)}
    return index


def collect_needs(plan: dict, deck_cfg: dict, data: dict, enrich: dict) -> list[dict]:
    rules = {r["role"]: r for r in deck_cfg["slides"]}
    index = source_index(data)
    events = {}
    for bucket in (data["events"].get("webinars") or {}).values():
        for ev in bucket:
            events[state.event_key(ev["url"])] = ev
    known = enrich.get("highlights") or {}
    known_events = enrich.get("events") or {}
    needs: dict[str, dict] = {}
    for slide in plan["slides"]:
        rule = rules.get(slide["role"], {})
        style = rule.get("style") or {}
        if slide["kind"] == "columns" and style.get("wording") != "label":
            for col in slide["columns"]:
                for entry in col["entries"]:
                    key, src = entry["key"], index.get(entry["key"])
                    if key in known or key in needs or not src:
                        continue
                    if "items" in src:  # a changelog day
                        if len(src["items"]) < 2 and not any(len(i["text"]) > 260 for i in src["items"]):
                            continue  # one short change reads fine verbatim
                        lines = [f"{i['title']}: {i['text']}" if i.get("title") else i["text"] for i in src["items"]]
                        needs[key] = {"id": key, "kind": "changelog", "product": src["_display"],
                                      "date": src["date"], "max_bullets": 1, "max_chars": MAX_CHARS["changelog"],
                                      "source": {"items": lines}}
                    elif not src.get("is_prerelease"):
                        notes = {name: [f"{i['lead']}: {i['text']}" if i.get("lead") else i["text"] for i in items]
                                 for name, items in (src.get("sections") or {}).items() if items}
                        if not any(name.strip().lower() in ADDS for name in notes):
                            continue
                        needs[key] = {"id": key, "kind": "release", "product": src["_display"],
                                      "version": src["version"], "date": src["date"],
                                      "max_bullets": int(style.get("bullets", 1)),
                                      "max_chars": MAX_CHARS["release"], "source": {"notes": notes}}
        elif slide["kind"] == "webinars":
            for slot in slide["slots"]:
                key = state.event_key(slot["url"])
                ev = events.get(key)
                if key in known_events or key in needs or not ev:
                    continue
                if ev.get("abstract_status") == "ok" and len(ev.get("abstract") or "") > EVENT_MIN_ABSTRACT:
                    needs[key] = {"id": key, "kind": "event", "title": ev["title"], "max_bullets": 1,
                                  "max_chars": MAX_CHARS["event"], "source": {"abstract": ev["abstract"]}}
    return list(needs.values())


def problems(need: dict, bullets) -> list[str]:
    """Why an answer for `need` cannot be used ([] = accept it)."""
    if not isinstance(bullets, list) or not 1 <= len(bullets) <= need["max_bullets"]:
        return [f"expected 1-{need['max_bullets']} bullet(s)"]
    source = json.dumps(need["source"], ensure_ascii=False)
    own = {str(need.get("version", "")), str(need.get("date", ""))}
    out = []
    for bullet in bullets:
        if not isinstance(bullet, str):
            return ["a bullet is not text"]
        plain = bullet.replace("**", "")
        if bullet.count("**") % 2 or bullet.count("**") > 4:
            out.append("unbalanced or excessive ** emphasis")
        if not MIN_CHARS <= len(plain) <= need["max_chars"]:
            out.append(f"{len(plain)} characters (allowed {MIN_CHARS}-{need['max_chars']})")
        if any(ch in bullet for ch in ("\n", "`", "—", "–")) or "http" in bullet.lower():
            out.append("line break, backtick, dash or URL")
        if not plain.rstrip().endswith((".", "!", "?")):
            out.append("does not end as a sentence")
        low = plain.lower()
        out += [f"banned wording '{w}'" for w in BANNED if w in low]
        out += [f"version {v} is not in the source" for v in VERSION_RE.findall(plain)
                if v not in source and v not in own]
        out += [f"{c} is not in the source" for c in CVE_RE.findall(plain) if c.upper() not in source.upper()]
        out += [f"figure {n} is not in the source" for n in NUMBER_RE.findall(VERSION_RE.sub(" ", plain))
                if n not in source and not any(n in o for o in own)]
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--plan", default=str(DATA_DIR / "deck-plan.json"))
    ap.add_argument("--dry-run", action="store_true", help="write the request and stop (no agent call)")
    args = ap.parse_args()

    model = os.environ.get("ENRICH_MODEL") or DEFAULT_MODEL
    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    data = {name: read_json(f"{name}.json") for name in ("releases", "changelogs", "events")}
    enrich = state.load("enrichment.json")
    needs = collect_needs(plan, load_config("deck.yaml"), data, enrich)
    report = {"model": model, "requested": len(needs), "accepted": [], "rejected": [], "status": "ok"}
    request = {"items": needs}
    (DATA_DIR / "enrich-request.json").write_text(json.dumps(request, indent=2, ensure_ascii=False) + "\n",
                                                  encoding="utf-8")

    def finish(status: str, note: str = "") -> int:
        report["status"] = status
        if note:
            report["note"] = note
            log(f"enrich: {note}")
        (DATA_DIR / "enrich-report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                                                     encoding="utf-8")
        return 0

    if not needs:
        return finish("nothing-to-do", "every visible item already has stored wording")
    log(f"enrich: {len(needs)} item(s) without stored wording: {[n['id'][-48:] for n in needs]}")
    if args.dry_run:
        return finish("dry-run", "request written to data/enrich-request.json")
    if not agent.available():
        return finish("skipped", "opencode is not installed; using rule-based wording")

    scratch = Path(tempfile.mkdtemp(prefix="enrich-"))
    try:
        (scratch / "enrich-request.json").write_text(json.dumps(request, indent=2, ensure_ascii=False),
                                                     encoding="utf-8")
        code = agent.run("deck-enrich", "Process enrich-request.json and write enrich-response.json.",
                         model=model, cwd=scratch, timeout=int(os.environ.get("ENRICH_TIMEOUT", "600")),
                         log_file=DATA_DIR / "enrich-agent.log",
                         drop_env=("GIT_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_TOKEN"))
        answer_file = scratch / "enrich-response.json"
        if code != 0 or not answer_file.exists():
            return finish("failed", f"agent exited {code} without a usable answer (see data/enrich-agent.log); "
                                    "using rule-based wording")
        shutil.copy(answer_file, DATA_DIR / "enrich-response.json")
        try:
            answer = json.loads(answer_file.read_text(encoding="utf-8"))
            answers = {a["id"]: a.get("bullets") for a in answer["items"]}
        except (ValueError, KeyError, TypeError) as exc:
            return finish("failed", f"answer is not the expected JSON ({exc}); using rule-based wording")
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    stamp = {"source": f"llm:{model}", "written": today().isoformat()}
    for need in needs:
        bullets = answers.get(need["id"])
        why = ["no answer for this item"] if bullets is None else problems(need, bullets)
        if why:
            report["rejected"].append({"id": need["id"], "why": why, "bullets": bullets})
            log(f"  rejected {need['id'][-60:]}: {'; '.join(why)}")
            continue
        if need["kind"] == "event":
            enrich.setdefault("events", {})[need["id"]] = {"description": bullets[0], **stamp}
        else:
            enrich.setdefault("highlights", {})[need["id"]] = {"bullets": bullets, **stamp}
        report["accepted"].append({"id": need["id"], "bullets": bullets})
        log(f"  accepted {need['id'][-60:]}: {bullets[0][:90]}")
    if report["accepted"]:
        state.save("enrichment.json", enrich)
    return finish("ok", f"{len(report['accepted'])} accepted, {len(report['rejected'])} rejected")


if __name__ == "__main__":
    raise SystemExit(main())
