"""Call the pipeline's LLM gateways, and ask them whether they accept the pipeline's keys.

Models are named as opencode names them ("<provider>/<model id>"): `ica-gateway/...` is the ICA
gateway, OpenAI-compatible (ICA_BASE_URL, ICA_API_KEY); `bob/shell` is IBM Bob through Bob Shell 2
(lib/bob.py, BOB_API_KEY), which picks its own model. Bob is every step's last resort: with_last_resort()
adds it after a step's own models whenever its key is set, for the day the ICA gateway is down.

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
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

# The gateways' Cloudflare turns away urllib's default user agent (and Bob's, one naming opencode).
USER_AGENT = "auto-pptx-pipeline"
# provider -> (base URL variable, its default, key variable, Authorization scheme). Bob is reached through
# Bob Shell, not over HTTP: its firewall turns other clients away.
PROVIDERS = {
    "ica-gateway": ("ICA_BASE_URL", "", "ICA_API_KEY", "Bearer"),
    "bob": ("", "bob-shell", "BOB_API_KEY", ""),
}
LAST_RESORT = "bob/shell"
# Bob Shell is an agent: even with every tool group turned off it may try to read files, and then answers
# with the tool calls it could not make.
NO_TOOLS = ("You answer in this one message. You have no tools and no files: everything you need is below. "
            "Do not try to read, search or list anything.\n\n")


def with_last_resort(models: list[str], env=None) -> list[str]:
    """`models`, then Bob when its key is set (BOB_FALLBACK=0 leaves it out)."""
    env = os.environ if env is None else env
    models = [m for m in dict.fromkeys(models) if m]
    if env.get("BOB_API_KEY") and env.get("BOB_FALLBACK", "1") != "0" and not any(m.startswith("bob/") for m in models):
        models.append(LAST_RESORT)
    return models


def endpoint(model: str, env=None) -> tuple[str, str, str, str] | None:
    """(base URL, key, Authorization scheme, key variable) for "<provider>/<model id>", or None when
    the provider is unknown or its key or URL is not set."""
    env = os.environ if env is None else env
    provider = model.partition("/")[0]
    if provider not in PROVIDERS:
        return None
    url_var, default, key_var, scheme = PROVIDERS[provider]
    base, key = (env.get(url_var) if url_var else "") or default, env.get(key_var, "")
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
            title = re.search(r"<title>(.*?)</title>", text, re.S | re.I)
            if title:   # a firewall's or proxy's page, not the API's answer
                text = f"{' '.join(title.group(1).split())} (an HTML page, not the API)"
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
    if model.partition("/")[0] == "bob":
        return ask_bob(NO_TOOLS + "\n\n---\n\n".join(str(m.get("content", "")) for m in messages), env,
                       timeout=max(timeout, 600))
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


def ask_bob(prompt: str, env=None, *, timeout: int = 600) -> tuple[int | None, str]:
    """One question to Bob Shell, in ask mode, in an empty directory, with no tools at all."""
    env = os.environ if env is None else env
    from lib import bob
    with tempfile.TemporaryDirectory(prefix="bob-ask-") as workspace:
        status, text, _ = bob.run(prompt, workspace=Path(workspace), mode="ask", tools=(), max_turns=4,
                                  max_cost=float(env.get("BOB_MAX_COST") or 1), timeout=timeout, env=env)
    return status, text


def _clean(text: str, key: str, key_var: str = "ICA_API_KEY") -> str:
    text = text.split("Received API Key")[0].replace(key, f"[{key_var} removed]")
    text = re.sub(r"\bsk-\S+|\b[0-9a-f]{32,}\b", "[removed]", text)
    return " ".join(text.split()).rstrip(" .,")[:160]


def preflight(env=None) -> int:
    """Print whether the gateways accept their keys for ENRICH_MODEL, FIX_MODEL, their fallbacks and the
    REVIEW_MODELS.
    Returns 1 when a model whose key is set is not confirmed; a missing key is not a failure (the build
    keeps its rule-based wording, self-heal reports instead of repairing, the review falls back or says
    it could not run)."""
    env = os.environ if env is None else env
    models = with_last_resort([env.get("ENRICH_MODEL"), env.get("FIX_MODEL"), env.get("ENRICH_FALLBACK_MODEL"),
                               env.get("FIX_FALLBACK_MODEL"), *(env.get("REVIEW_MODELS") or "").split()], env)
    unconfirmed = 0
    for model in models:
        found = endpoint(model, env)
        if not found:
            continue
        base, key, scheme, key_var = found
        if model.startswith("bob/"):
            from lib import bob
            if bob.executable() is None and env.get("PIPELINE_MODE") != "preflight":
                print(f"  {model:<32} not checked: Bob Shell is installed when it is needed (the preflight checks it)")
                continue
            status, said = ask_bob("Reply with the single word OK.", env)
        else:
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
