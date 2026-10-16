"""Shared helpers for the auto-pptx fetchers."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = REPO_ROOT / "config"
# Fetched data and reports. AUTO_PPTX_DATA_DIR redirects them (the tests build from a saved snapshot
# in a scratch directory; a pipeline can keep them on a workspace volume).
DATA_DIR = Path(os.environ.get("AUTO_PPTX_DATA_DIR") or REPO_ROOT / "data").resolve()

USER_AGENT = (
    "auto-pptx/1.0 (+https://github.com; HashiCorp cadence deck automation; contact: internal)"
)


MONTH_NAMES = ["January", "February", "March", "April", "May", "June",
               "July", "August", "September", "October", "November", "December"]


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", file=sys.stderr)


def today() -> date:
    """The run's "today". BUILD_DATE (YYYY-MM-DD) pins it so a build can be reproduced or tested
    against saved data; unset, it is the real date."""
    pinned = os.environ.get("BUILD_DATE", "").strip()
    return date.fromisoformat(pinned) if pinned else date.today()


def window_start(months: int, ref: date | None = None) -> str:
    """ISO date of the first day of the month `months - 1` months before `ref`'s month, i.e. the
    start of a window that covers `months` calendar months including the current one."""
    ref = ref or today()
    idx = ref.year * 12 + (ref.month - 1) - (months - 1)
    return date(idx // 12, idx % 12 + 1, 1).isoformat()


def month_label(iso: str) -> str | None:
    """'2026-06-17' -> 'June' (bare English month name, matching the deck's column headers)."""
    try:
        return MONTH_NAMES[int(iso[5:7]) - 1]
    except (ValueError, IndexError, TypeError):
        return None


def read_json(name: str) -> dict:
    """data/<name> as a dict, or {} when the file is missing."""
    path = DATA_DIR / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def load_config(name: str = "sources.yaml") -> dict:
    if yaml is None:
        raise RuntimeError("PyYAML is required. Install with: pip install pyyaml")
    with open(CONFIG_DIR / name, "r", encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def http_get(url: str, *, retries: int = 4, backoff: float = 3.0, timeout: int = 25) -> bytes:
    """GET with retry/backoff. Raises the last error if all attempts fail."""
    return http_get_final(url, retries=retries, backoff=backoff, timeout=timeout)[0]


def http_get_final(url: str, *, retries: int = 4, backoff: float = 3.0,
                   timeout: int = 25) -> tuple[bytes, str]:
    """GET like http_get, also returning the URL the request ended on after redirects (docs pages
    move; links written into the deck should point at where a page lives now)."""
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read(), resp.geturl()
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
