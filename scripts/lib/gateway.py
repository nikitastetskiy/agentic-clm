"""Ask the LLM gateway whether it accepts the pipeline's key for the models the pipeline uses.

The weekly build only calls a model when something needs new wording, and self-heal only when a build
fails, so a rotated key or a model the gateway stopped serving stays hidden behind green runs until the
day it is needed. The preflight (ci/lib.sh) runs this check with the smallest request the model steps
make. The key is only sent in the Authorization header, and the gateway's error text is cut short:
its 401 echoes the key's last characters and its hash.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

USER_AGENT = "auto-pptx-pipeline"   # the gateway's Cloudflare turns away urllib's default user agent


def check(base_url: str, key: str, model: str, timeout: int = 60) -> tuple[int | None, str]:
    """(HTTP status, or None when nothing answered; what was said) for one "<provider>/<model id>" as
    opencode names it."""
    body = json.dumps({"model": model.partition("/")[2] or model, "max_tokens": 1,
                       "messages": [{"role": "user", "content": "Reply with OK."}]}).encode()
    try:
        req = urllib.request.Request(f"{base_url.rstrip('/')}/chat/completions", data=body, method="POST",
                                     headers={"Authorization": f"Bearer {key}", "User-Agent": USER_AGENT,
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, ""
    except urllib.error.HTTPError as exc:
        with exc:
            text = exc.read().decode("utf-8", "replace")
        try:
            text = json.loads(text)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            pass
        return exc.code, _clean(text, key)
    except (OSError, ValueError) as exc:   # unreachable, timed out, or not a URL
        return None, _clean(str(getattr(exc, "reason", exc)), key)


def _clean(text: str, key: str) -> str:
    text = text.split("Received API Key")[0].replace(key, "[ICA_API_KEY removed]")
    text = re.sub(r"\bsk-\S+|\b[0-9a-f]{32,}\b", "[removed]", text)
    return " ".join(text.split()).rstrip(" .,")[:160]


def preflight(env=None) -> int:
    """Print whether the gateway accepts the key for ENRICH_MODEL and FIX_MODEL. Returns 1 when a key
    is set and either is not confirmed; no key at all is not a failure (the build keeps its rule-based
    wording, and self-heal reports instead of repairing)."""
    env = os.environ if env is None else env
    key, base = env.get("ICA_API_KEY", ""), env.get("ICA_BASE_URL", "")
    if not (key and base):
        return 0
    unconfirmed = 0
    for model in dict.fromkeys(m for m in (env.get("ENRICH_MODEL"), env.get("FIX_MODEL")) if m):
        status, said = check(base, key, model)
        if status is not None and status < 300:
            print(f"  {model:<32} accepted by the gateway")
            continue
        unconfirmed += 1
        verdict = f"REFUSED by the gateway (HTTP {status}: {said})" if status else f"NO ANSWER from the gateway ({said})"
        print(f"  {model:<32} {verdict}")
        if env.get("GITHUB_ACTIONS") == "true":   # shown on the run's page, not only in its log
            print(f"::warning::ICA_API_KEY not confirmed for {model}: {verdict}")
    return 1 if unconfirmed else 0
