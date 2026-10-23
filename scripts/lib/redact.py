"""Keep the pipeline's secrets out of everything it publishes.

The CI masks secrets in its own logs, but not in what the pipeline writes elsewhere: a pull request or
issue, a release's notes, a commit, the files saved with a run. In the self-heal run a model with a
shell works next to the gateway key (it needs the key to call the model), so it could be talked into
writing the key into one of those. Two defences use this module:

  * scrub()       — everything sent to GitHub (lib/github.py), the agent transcripts (lib/agent.py)
                    and the files a run keeps (scripts/scrub_secrets.py) have each secret's value
                    replaced by "[<NAME> removed]";
  * found_in()    — a proposed repair that contains a secret's value is rejected (check_fix.py), and
                    a release that would carry one is not published (publish_release.py).

A deck is a ZIP archive whose parts are compressed, so a value written into a slide is not in the
file's bytes: files that are archives are searched and scrubbed part by part.

Values are matched verbatim: this stops a key being pasted somewhere, not a model that encodes it
first. That is why, on GitHub, the repair agent and the code it writes never get the keys at all
(lib/sandbox.py), and why the gateway key should be one issued for the pipeline alone, with a spend
limit.
"""
from __future__ import annotations

import io
import os
import re
import zipfile
from pathlib import Path

SECRET_VARS = ("ICA_API_KEY", "BOB_API_KEY", "GIT_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_TOKEN",
               "SELFHEAL_WEBHOOK_URL", "SELFHEAL_WEBHOOK_TOKEN", "IBMCLOUD_API_KEY")
MIN_LENGTH = 8   # shorter values (the dry run's "dry-run" placeholder) would also match ordinary words
# Variables that hold a credential by their name alone, whoever set them: kept from Bob Shell
# (lib/bob.py) and from the sandbox (lib/sandbox.py).
SECRET_NAME = re.compile(r"KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|SESSION|COOKIE|WEBHOOK", re.I)
NESTING = 3   # archives inside archives (a workbook embedded in a slide) are opened this deep


def secrets(env=None) -> list[tuple[str, str]]:
    """(variable name, value) of every secret set in `env` (default: this process's environment)."""
    env = os.environ if env is None else env
    return [(name, env[name]) for name in SECRET_VARS if len(env.get(name) or "") >= MIN_LENGTH]


def scrub(text: str, env=None) -> str:
    for name, value in secrets(env):
        text = text.replace(value, f"[{name} removed]")
    return text


def scrub_bytes(data: bytes, env=None, depth: int = NESTING) -> bytes:
    """`data` with every secret's value replaced, inside archives too (their parts are compressed)."""
    if depth and _is_zip(data):
        data = _scrub_zip(data, env, depth)
    for name, value in secrets(env):
        data = data.replace(value.encode(), f"[{name} removed]".encode())
    return data


def _is_zip(data: bytes) -> bool:
    return data[:4] == b"PK\x03\x04" and zipfile.is_zipfile(io.BytesIO(data))


def _scrub_zip(data: bytes, env, depth: int) -> bytes:
    with zipfile.ZipFile(io.BytesIO(data)) as zin:
        parts = [(info, zin.read(info)) for info in zin.infolist()]
        comment = zin.comment
    clean = [(info, scrub_bytes(part, env, depth - 1)) for info, part in parts]
    if all(a is b or a == b for (_, a), (_, b) in zip(parts, clean)):
        return data
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as zout:   # same parts, order, dates and compression
        zout.comment = comment
        for info, part in clean:
            zout.writestr(info, part)
    return out.getvalue()


def _contents(data: bytes, depth: int = NESTING):
    """`data`, and the parts of the archive it is, if it is one."""
    yield data
    if depth and _is_zip(data):
        with zipfile.ZipFile(io.BytesIO(data)) as zin:
            for info in zin.infolist():
                yield from _contents(zin.read(info), depth - 1)


def scrub_tree(root: Path, env=None) -> list[Path]:
    """Scrub every file under `root` in place; return the files that held a secret."""
    changed = []
    for path in sorted(p for p in Path(root).rglob("*") if p.is_file() and not p.is_symlink()):
        data = path.read_bytes()
        clean = scrub_bytes(data, env)
        if clean != data:
            path.write_bytes(clean)
            changed.append(path)
    return changed


def found_in_file(path: Path, env=None) -> list[str]:
    """Names of the secrets whose value is in the file at `path`, inside its archive parts included."""
    found = set()
    for data in _contents(Path(path).read_bytes()):
        found.update(name for name, value in secrets(env) if value.encode() in data)
    return sorted(found)


def found_in(texts, env=None) -> list[str]:
    """Names of the secrets whose value occurs in any of `texts`."""
    texts = list(texts)
    return [name for name, value in secrets(env) if any(value in text for text in texts)]
