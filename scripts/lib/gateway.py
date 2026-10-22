"""Call the pipeline's LLM gateways, and ask them whether they accept the pipeline's keys.

Two gateways, both OpenAI-compatible, named as opencode names them ("<provider>/<model id>"):
`ica-gateway` (ICA_BASE_URL, ICA_API_KEY) and IBM Bob's inference API (`bob`, BOB_API_KEY: an
"Inference" key, tied to one Bob instance and team, so it needs no instance or team headers).

The weekly build only calls a model when something needs new wording, self-heal only when a build
fails, and the pull request review only when there is a pull request, so a rotated key or a model the
gateway stopped serving stays hidden behind green runs until the day it is needed. The preflight
(ci/lib.sh) runs this check with the smallest request the model steps make. A key is only sent in the
Authorization header, and a gateway's error text is cut short: the ICA gateway's 401 echoes the key's
last characters and its hash.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request

# The gateways' Cloudflare turns away urllib's default user agent (and Bob's, one naming opencode).
USER_AGENT = "auto-pptx-pipeline"
BOB_INFERENCE_URL = "https://api.us-east.bob.ibm.com/inference/v1"
# provider -> (base URL variable, its default, key variable, Authorization scheme)
PROVIDERS = {
    "ica-gateway": ("ICA_BASE_URL", "", "ICA_API_KEY", "Bearer"),
    "bob": ("BOB_BASE_URL", BOB_INFERENCE_URL, "BOB_API_KEY", "Apikey"),
}


def endpoint(model: str, env=None) -> tuple[str, str, str, str] | None:
    """(base URL, key, Authorization scheme, key variable) for "<provider>/<model id>", or None when
    the provider is unknown or its key or URL is not set."""
    env = os.environ if env is None else env
    provider = model.partition("/")[0]
    if provider not in PROVIDERS:
        return None
    url_var, default, key_var, scheme = PROVIDERS[provider]
    base, key = env.get(url_var) or default, env.get(key_var, "")
    return (base, key, scheme, key_var) if base and key else None


def _post(base_url: str, key: str, scheme: str, body: dict, timeout: int) -> tuple[int, bytes]:
    req = urllib.request.Request(f"{base_url.rstrip('/')}/chat/completions", data=json.dumps(body).encode(),
                                 method="POST", headers={"Authorization": f"{scheme} {key}",
                                                         "User-Agent": USER_AGENT,
                                                         "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


def _refusal(exc: Exception, key: str, key_var: str) -> tuple[int | None, str]:
    if isinstance(exc, urllib.error.HTTPError):
        with exc:
            text = exc.read().decode("utf-8", "replace")
        try:
            text = json.loads(text)["error"]["message"]
        except (ValueError, KeyError, TypeError):
            pass
        return exc.code, _clean(text, key, key_var)
    return None, _clean(str(getattr(exc, "reason", exc)), key, key_var)   # unreachable, timed out, not a URL


def check(base_url: str, key: str, model: str, timeout: int = 60, *, scheme: str = "Bearer",
          key_var: str = "ICA_API_KEY") -> tuple[int | None, str]:
    """(HTTP status, or None when nothing answered; what was said) for one "<provider>/<model id>"."""
    body = {"model": model.partition("/")[2] or model, "max_tokens": 1,
            "messages": [{"role": "user", "content": "Reply with OK."}]}
    try:
        return _post(base_url, key, scheme, body, timeout)[0], ""
    except (OSError, ValueError) as exc:
        return _refusal(exc, key, key_var)


def complete(model: str, messages: list[dict], env=None, *, max_tokens: int = 16000,
             timeout: int = 300) -> tuple[int | None, str]:
    """(200, the reply's text) from "<provider>/<model id>", or (HTTP status or None, what went wrong).
    max_tokens leaves room for a reasoning model's thinking, which counts towards it."""
    found = endpoint(model, env)
    if not found:
        return None, f"no {PROVIDERS.get(model.partition('/')[0], ('', '', 'key for this provider'))[2]} set"
    base, key, scheme, key_var = found
    body = {"model": model.partition("/")[2], "max_tokens": max_tokens, "messages": messages}
    try:
        status, raw = _post(base, key, scheme, body, timeout)
    except (OSError, ValueError) as exc:
        return _refusal(exc, key, key_var)
    try:
        content = json.loads(raw)["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return status, "the reply had no message"
    if isinstance(content, list):   # content parts
        content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return status, content if isinstance(content, str) else ""


def _clean(text: str, key: str, key_var: str = "ICA_API_KEY") -> str:
    text = text.split("Received API Key")[0].replace(key, f"[{key_var} removed]")
    text = re.sub(r"\bsk-\S+|\b[0-9a-f]{32,}\b", "[removed]", text)
    return " ".join(text.split()).rstrip(" .,")[:160]


def preflight(env=None) -> int:
    """Print whether the gateways accept their keys for ENRICH_MODEL, FIX_MODEL and the REVIEW_MODELS.
    Returns 1 when a model whose key is set is not confirmed; a missing key is not a failure (the build
    keeps its rule-based wording, self-heal reports instead of repairing, the review falls back or says
    it could not run)."""
    env = os.environ if env is None else env
    models = [env.get("ENRICH_MODEL"), env.get("FIX_MODEL"), *(env.get("REVIEW_MODELS") or "").split()]
    unconfirmed = 0
    for model in dict.fromkeys(m for m in models if m):
        found = endpoint(model, env)
        if not found:
            continue
        base, key, scheme, key_var = found
        status, said = check(base, key, model, scheme=scheme, key_var=key_var)
        if status is not None and status < 300:
            print(f"  {model:<32} accepted by the gateway")
            continue
        unconfirmed += 1
        verdict = f"REFUSED by the gateway (HTTP {status}: {said})" if status else f"NO ANSWER from the gateway ({said})"
        print(f"  {model:<32} {verdict}")
        if env.get("GITHUB_ACTIONS") == "true":   # shown on the run's page, not only in its log
            print(f"::warning::{key_var} not confirmed for {model}: {verdict}")
    return 1 if unconfirmed else 0
