#!/usr/bin/env python3
"""Have the enrichment agent word what nobody has worded yet, once, and keep it.

The plan (plan_deck.py) fills every slide from fixed rules. For a NEW release with features, or a
changelog day with several changes, a rule can only quote the first note; a person would write one
sentence that sums the release up. This step asks a small model for that sentence
and stores it in state/enrichment.json, so:

  * each item is worded ONCE — later builds reuse the stored text instead of rewording the deck
    every week (and a person can correct it by editing the state file);
  * the build itself stays deterministic: plan + build + verify never call a model.

The model is given only public text (release notes, changelog entries, event abstracts) and its
answer is treated as untrusted data: a bullet is accepted only if it is short enough, plainly worded, and every version
number, figure and CVE id in it occurs in the source it was given. Anything else is dropped and the
deck falls back to the rule-based wording.

Two ways to reach the model, with the same instructions (.opencode/agents/deck-enrich.md), models and
rules: with the gateway's key and address in the environment (ICA_API_KEY, ICA_BASE_URL: every pipeline
run) a direct call, which gives the model no tools at all; without them (a laptop), the `deck-enrich`
opencode agent with opencode's own login, in a scratch directory with no shell and no network.

Best-effort by design: no model, no credentials, a timeout or a bad answer never fails a build.
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
from lib import agent, gateway, state  # noqa: E402
from lib.common import DATA_DIR, REPO_ROOT, load_config, log, read_json, today  # noqa: E402

DEFAULT_MODEL = "ica-gateway/claude-haiku-5-5"
DEFAULT_FALLBACK_MODEL = "ica-gateway/gpt-6-luna"   # another vendor's, when the first gives no answer
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


def models(env=None) -> list[str]:
    """The enrichment models in order: ENRICH_MODEL, then ENRICH_FALLBACK_MODEL ("none" for no fallback)."""
    env = os.environ if env is None else env
    fallback = env.get("ENRICH_FALLBACK_MODEL") or DEFAULT_FALLBACK_MODEL
    return list(dict.fromkeys([env.get("ENRICH_MODEL") or DEFAULT_MODEL, *([] if fallback == "none" else [fallback])]))


def parse_answer(text) -> dict | None:
    """{id: bullets} from an answer, or None when it is not the JSON asked for."""
    m = re.search(r"\{.*\}", text, re.S) if isinstance(text, str) else None
    try:
        return {a["id"]: a.get("bullets") for a in json.loads(m.group(0))["items"]} if m else None
    except (ValueError, KeyError, TypeError, AttributeError):
        return None


def read_answer(path: Path) -> dict | None:
    """{id: bullets} from the agent's answer file, or None when there is none or it is not the JSON asked for."""
    try:
        return parse_answer(path.read_text(encoding="utf-8"))
    except OSError:
        return None


def direct_prompt(request: dict) -> list[dict]:
    """The deck-enrich agent's instructions as a direct call: the request is the message, the reply the answer."""
    text = (REPO_ROOT / ".opencode" / "agents" / "deck-enrich.md").read_text(encoding="utf-8")
    body = text.split("---", 2)[2].strip() if text.startswith("---") else text
    system = (f"{body}\n\nHere you are called directly, without files: the user message is the content of "
              "`enrich-request.json`, and your reply is the content of `enrich-response.json`, the JSON object only.")
    return [{"role": "system", "content": system},
            {"role": "user", "content": json.dumps(request, indent=2, ensure_ascii=False)}]


def ask_direct(request: dict, env, complete=gateway.complete) -> tuple[dict | None, str, list[str]]:
    """(answers, the model that gave them or the last one asked, why the ones before it did not)."""
    failures, model = [], ""
    for model in models(env):
        status, text = complete(model, direct_prompt(request), env)
        answers = parse_answer(text) if status == 200 else None
        if answers is not None:
            return answers, model, failures
        failures.append(f"{model}: " + ("the answer was not the JSON asked for" if status == 200
                                        else f"HTTP {status}: {text}" if status else text))
    return None, model, failures


def ask_agent(request: dict) -> tuple[dict | None, str]:
    """(answers, the model that ran last) from the deck-enrich opencode agent: the laptop's way."""
    scratch = Path(tempfile.mkdtemp(prefix="enrich-"))
    try:
        (scratch / "enrich-request.json").write_text(json.dumps(request, indent=2, ensure_ascii=False),
                                                     encoding="utf-8")
        answer_file = scratch / "enrich-response.json"

        def answered(code: int) -> bool:
            if read_answer(answer_file) is not None:
                return True
            answer_file.unlink(missing_ok=True)   # the next model starts without it
            return False

        _, model = agent.run_first(
            "deck-enrich", "Process enrich-request.json and write enrich-response.json.", models=models(),
            answered=answered, cwd=scratch, timeout=int(os.environ.get("ENRICH_TIMEOUT", "600")),
            log_file=DATA_DIR / "enrich-agent.log",
            drop_env=("GIT_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_TOKEN", "BOB_API_KEY"))
        answers = read_answer(answer_file)
        if answers is not None:
            shutil.copy(answer_file, DATA_DIR / "enrich-response.json")
        return answers, model
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--plan", default=str(DATA_DIR / "deck-plan.json"))
    ap.add_argument("--dry-run", action="store_true", help="write the request and stop (no agent call)")
    args = ap.parse_args()

    model = models()[0]
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
    if gateway.endpoint(model, os.environ):
        report["via"] = "the gateway, directly"
        answers, model, failures = ask_direct(request, os.environ)
        for failure in failures:
            log(f"enrich: {failure}")
        if failures:
            report["failures"] = failures
        if answers is not None:
            (DATA_DIR / "enrich-response.json").write_text(json.dumps(answers, indent=2, ensure_ascii=False) + "\n",
                                                           encoding="utf-8")
    elif agent.available():
        report["via"] = "the deck-enrich opencode agent"
        answers, model = ask_agent(request)
    else:
        return finish("skipped", "no gateway key in the environment and no opencode; using rule-based wording")
    report["model"] = model
    if answers is None:
        return finish("failed", f"no model gave a usable answer (last: {model}); using rule-based wording")

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
