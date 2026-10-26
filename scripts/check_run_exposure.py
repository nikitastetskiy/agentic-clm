#!/usr/bin/env python3
"""Look for a token in what a finished GitHub Actions run left behind, and take it down.

    python3 scripts/check_run_exposure.py --repo OWNER/NAME --run RUN_ID [--look-only]

GitHub masks a secret in a run's log only where its exact value is printed whole. A value cut in two
or encoded, or a credential GitHub was never told about (a key in a model's answer, another tool's
token), stays readable by everyone who can open the run. lib/redact.py keeps the values the pipeline
knows out of what it publishes; this check reads what each run actually left (its logs, its artifacts
and the release it published) after it ends (.github/workflows/exposure-check.yml), and looks for

  * the pipeline's secrets: any PIECE characters of a value in a row, or the value in base64;
  * anything shaped like a credential, whosever it is (SHAPES). The fakes in tests/ do not count.

A find fails the check, deletes the run's logs and every file that held one, and opens an issue that
says where and what, never the value. Deleting is not un-seeing: the key must still be rotated.
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import re
import subprocess
import sys
import time
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib import redact  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PIECE = 12   # the shortest run of a secret's characters that counts as the secret showing
SHAPES = {
    "an ICA (LiteLLM) key": rb"(?<![\w-])sk-[A-Za-z0-9_-]{20,}",
    "a Bob key": rb"(?<![\w-])bob_[A-Za-z0-9_-]{40,}",
    "a GitHub token": rb"(?<![\w-])(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{40,})",
    "a private key": rb"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "an AWS access key": rb"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])",
    "a Slack token": rb"(?<![\w-])xox[abposr]-[A-Za-z0-9-]{10,}",
    "a credential in a header": rb"(?i)(?:authorization|apikey|x-api-key)[\"']?\s*[:=]\s*[\"']?(?:bearer\s+|token\s+|basic\s+)?[A-Za-z0-9._~+/=-]{16,}",
}
NESTING = 2   # a run artifact is a ZIP of files, and a deck inside it is a ZIP of parts


def needles(env=None) -> list[tuple[str, bytes]]:
    """(secret's name, bytes that only that secret has): its pieces, and its base64 at each alignment."""
    found = []
    for name, value in redact.secrets(env):
        raw = value.encode()
        found += [(name, raw[i:i + PIECE]) for i in range(max(len(raw) - PIECE, 0) + 1)]
        for shift in range(3):
            encoded = base64.b64encode(b"\0" * shift + raw)
            core = encoded[4 if shift else 0:-4]   # what does not depend on the bytes around the value
            if len(core) >= PIECE:
                found.append((f"{name} (base64)", core))
    return found


def fakes() -> set[bytes]:
    """The credential-shaped strings the repository's own tests use."""
    text = b"\n".join(p.read_bytes() for p in (ROOT / "tests").rglob("*.py"))
    return {m.group() for pattern in SHAPES.values() for m in re.finditer(pattern, text)}


def files(name: str, data: bytes, depth: int = NESTING):
    """(name, bytes) of `data` and, if it is a ZIP archive, of each file in it."""
    yield name, data
    if depth and data[:4] == b"PK\x03\x04" and zipfile.is_zipfile(io.BytesIO(data)):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                yield from files(f"{name}/{info.filename}", z.read(info), depth - 1)


def scan(name: str, data: bytes, secret: list[tuple[str, bytes]], known_fakes: set[bytes]) -> list[str]:
    """Where `data` (and the files in it) shows a token, as 'file, line N: what'; never the token."""
    finds = []
    for where, blob in files(name, data):
        hits = {}
        for what, needle in secret:
            at = blob.find(needle)
            if at >= 0:
                hits.setdefault(what, at)
        for what, pattern in SHAPES.items():
            at = next((m.start() for m in re.finditer(pattern, blob) if m.group() not in known_fakes), -1)
            if at >= 0:
                hits.setdefault(what, at)
        for what, at in hits.items():
            line = blob.count(b"\n", 0, at) + 1
            finds.append(f"{where}, line {line}: {what}")
    return finds


# --------------------------------------------------------------------------- GitHub ---------------
def gh(*args: str) -> bytes:
    return subprocess.run(["gh", *args], capture_output=True, check=True).stdout


def gh_json(path: str):
    return json.loads(gh("api", path))


def run_logs(repo: str, run: str) -> bytes:
    for attempt in range(6):   # a run's logs are ready a little after it is reported complete
        try:
            return gh("api", f"repos/{repo}/actions/runs/{run}/logs")
        except subprocess.CalledProcessError:
            if attempt == 5:
                raise
            time.sleep(10)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repo", required=True)
    ap.add_argument("--run", required=True)
    ap.add_argument("--look-only", action="store_true", help="report, delete nothing, open no issue")
    args = ap.parse_args()
    repo, run = args.repo, args.run
    info = gh_json(f"repos/{repo}/actions/runs/{run}")
    secret, known_fakes = needles(), fakes()

    finds, delete = [], []
    logs = scan("logs", run_logs(repo, run), secret, known_fakes)
    finds += logs
    for art in gh_json(f"repos/{repo}/actions/runs/{run}/artifacts?per_page=100")["artifacts"]:
        if art["expired"]:
            continue
        hit = scan(f"artifact {art['name']}", gh("api", f"repos/{repo}/actions/artifacts/{art['id']}/zip"),
                   secret, known_fakes)
        finds += hit
        delete += [f"repos/{repo}/actions/artifacts/{art['id']}"] if hit else []
    for rel in gh_json(f"repos/{repo}/releases?per_page=20"):
        if not info["run_started_at"] <= rel["created_at"] <= info["updated_at"]:
            continue   # only the release this run published
        for asset in rel["assets"]:
            hit = scan(f"release {rel['tag_name']}: {asset['name']}",
                       gh("api", "-H", "Accept: application/octet-stream",
                          f"repos/{repo}/releases/assets/{asset['id']}"), secret, known_fakes)
            finds += hit
            delete += [f"repos/{repo}/releases/assets/{asset['id']}"] if hit else []

    label = f"{info['name']} run {run} ({info['html_url']})"
    if not finds:
        print(f"no token in {label}: logs, artifacts and release checked "
              f"for {', '.join(sorted({n for n, _ in secret})) or 'no secrets set'} and {len(SHAPES)} shapes")
        return 0
    print(f"TOKEN EXPOSED in {label}:")
    for f in finds:
        print(f"  {f}")
    if args.look_only:
        return 1
    removed = []
    if logs:
        gh("api", "-X", "DELETE", f"repos/{repo}/actions/runs/{run}/logs")
        removed.append("the run's logs")
    for path in delete:
        gh("api", "-X", "DELETE", path)
        removed.append(path.split(f"repos/{repo}/")[1])
    body = "\n".join([
        f"The token exposure check found a credential in {label}:", "",
        *[f"- {f}" for f in finds], "",
        f"Deleted: {', '.join(removed)}.", "",
        "**Rotate the key now**: anyone who opened the run before this check could have read it. "
        "Store the new key in the keychain and set the secret by pipe (docs/pipelines.md, "
        "\"Token exposure check\"), then close this issue."])
    gh("issue", "create", "-R", repo, "--title", f"Token exposed in {info['name']} run {run}",
       "--body", redact.scrub(body))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
