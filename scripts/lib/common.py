"""Shared helpers for the auto-pptx fetchers."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
DATA_DIR = REPO_ROOT / "data"

USER_AGENT = (
    "auto-pptx/1.0 (+https://github.com; HashiCorp cadence deck automation; contact: internal)"
)


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr)


def load_config(name: str = "sources.yaml") -> dict:
    if yaml is None:
        raise RuntimeError("PyYAML is required. Install with: pip install pyyaml")
    with open(CONFIG_DIR / name, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def http_get(url: str, *, retries: int = 4, backoff: float = 3.0, timeout: int = 25) -> bytes:
    """GET with retry/backoff. Raises the last error if all attempts fail."""
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last_err = exc
            # 429/5xx are retryable; 4xx (except 429) are not.
            if exc.code != 429 and 400 <= exc.code < 500:
                raise
            log(f"  attempt {attempt}/{retries} -> HTTP {exc.code}; retrying in {backoff:.0f}s")
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_err = exc
            log(f"  attempt {attempt}/{retries} -> {exc}; retrying in {backoff:.0f}s")
        if attempt < retries:
            time.sleep(backoff * attempt)
    raise RuntimeError(f"GET failed after {retries} attempts: {url}: {last_err}")


def http_get_json(url: str, **kw) -> object:
    return json.loads(http_get(url, **kw).decode("utf-8"))


def write_output(filename: str, payload: dict) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"generated_at": datetime.now(timezone.utc).isoformat(), **payload}
    path = DATA_DIR / filename
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    log(f"wrote {path.relative_to(REPO_ROOT)}")
    return path
