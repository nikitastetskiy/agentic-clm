"""Keep the pipeline's secrets out of everything it publishes.

The CI masks secrets in its own logs, but not in what the pipeline writes elsewhere: a pull request or
issue, a release's notes, a commit, the files saved with a run. In the self-heal run a model with a
shell works next to the gateway key (it needs the key to call the model), so it could be talked into
writing the key into one of those. Two defences use this module:

  * scrub()       — everything sent to GitHub (lib/github.py), the agent transcripts (lib/agent.py)
                    and the files a run keeps (scripts/scrub_secrets.py) have each secret's value
                    replaced by "[<NAME> removed]";
  * found_in()    — a proposed repair that contains a secret's value is rejected (check_fix.py).

Values are matched verbatim: this stops a key being pasted somewhere, not a model that encodes it
first. That is why the gateway key should be one issued for the pipeline alone, with a spend limit.
"""
from __future__ import annotations

import os
from pathlib import Path

SECRET_VARS = ("ICA_API_KEY", "GIT_TOKEN", "GH_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_TOKEN",
               "SELFHEAL_WEBHOOK_URL", "SELFHEAL_WEBHOOK_TOKEN", "IBMCLOUD_API_KEY")
MIN_LENGTH = 8   # shorter values (the dry run's "dry-run" placeholder) would also match ordinary words


def secrets(env=None) -> list[tuple[str, str]]:
    """(variable name, value) of every secret set in `env` (default: this process's environment)."""
    env = os.environ if env is None else env
    return [(name, env[name]) for name in SECRET_VARS if len(env.get(name) or "") >= MIN_LENGTH]


def scrub(text: str, env=None) -> str:
    for name, value in secrets(env):
        text = text.replace(value, f"[{name} removed]")
    return text


def scrub_bytes(data: bytes, env=None) -> bytes:
    for name, value in secrets(env):
        data = data.replace(value.encode(), f"[{name} removed]".encode())
    return data


def scrub_tree(root: Path, env=None) -> list[Path]:
    """Scrub every file under `root` in place; return the files that held a secret."""
    changed = []
    for path in sorted(p for p in Path(root).rglob("*") if p.is_file()):
        data = path.read_bytes()
        clean = scrub_bytes(data, env)
        if clean != data:
            path.write_bytes(clean)
            changed.append(path)
    return changed


def found_in(texts, env=None) -> list[str]:
    """Names of the secrets whose value occurs in any of `texts`."""
    texts = list(texts)
    return [name for name, value in secrets(env) if any(value in text for text in texts)]
