"""Minimal GitHub REST client for the pipeline (releases, pull requests, issues).

Works against github.com and GitHub Enterprise Server (github.ibm.com) with nothing but the standard
library, so the pipeline image needs no `gh` CLI. The token comes from the environment (GIT_TOKEN,
filled from a pipeline secret) and is only ever sent in the Authorization header — it is never logged,
written to disk or put in a URL.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from lib.common import REPO_ROOT

TOKEN_VARS = ("GIT_TOKEN", "GH_ENTERPRISE_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")
REMOTE_RE = re.compile(r"^(?:git@|ssh://git@|https?://(?:[^@/]+@)?)([^:/]+)[:/](.+?)(?:\.git)?/?$")


class GitHubError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"GitHub API {status}: {message}")
        self.status = status


def remote_coordinates(remote: str = "origin") -> tuple[str, str]:
    """(host, "owner/repo") of the checkout's remote, from its ssh or https URL."""
    url = subprocess.run(["git", "-C", str(REPO_ROOT), "remote", "get-url", remote],
                         capture_output=True, text=True, check=True).stdout.strip()
    m = REMOTE_RE.match(url)
    if not m:
        raise GitHubError(0, f"cannot parse the git remote URL of '{remote}'")
    return m.group(1), m.group(2)


def token_from_env() -> str:
    for var in TOKEN_VARS:
        if os.environ.get(var):
            return os.environ[var]
    return ""


class GitHub:
    def __init__(self, host: str, repo: str, token: str, *, dry_run: bool = False):
        self.host, self.repo, self.token, self.dry_run = host, repo, token, dry_run
        self.api = "https://api.github.com" if host == "github.com" else f"https://{host}/api/v3"
        self.calls: list[str] = []   # what a dry run would have sent

    @classmethod
    def from_checkout(cls, *, dry_run: bool = False) -> "GitHub":
        host = os.environ.get("GITHUB_HOST")
        repo = os.environ.get("GITHUB_REPO")
        if not (host and repo):
            host, repo = remote_coordinates()
        return cls(host, repo, token_from_env(), dry_run=dry_run)

    def web_url(self, path: str = "") -> str:
        return f"https://{self.host}/{self.repo}{path}"

    def request(self, method: str, url: str, payload=None, *, data: bytes | None = None,
                content_type: str = "application/json"):
        if not url.startswith("http"):
            url = f"{self.api}/repos/{self.repo}{url}"
        if self.dry_run:
            # A rehearsal sends nothing at all: writes are listed, reads answer "nothing there yet".
            if method != "GET":
                self.calls.append(f"{method} {url}")
            return {}
        body = data if data is not None else (json.dumps(payload).encode() if payload is not None else None)
        req = urllib.request.Request(url, data=body, method=method, headers={
            "Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
            "Content-Type": content_type, "User-Agent": "auto-pptx-pipeline"})
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise GitHubError(exc.code, f"{method} {url.split('?')[0]}: {detail}") from None
        except urllib.error.URLError as exc:
            raise GitHubError(0, f"{method} {url.split('?')[0]}: {exc.reason}") from None
        return json.loads(raw) if raw else {}

    # ---- releases ------------------------------------------------------------------------------
    def release_by_tag(self, tag: str) -> dict | None:
        try:
            return self.request("GET", f"/releases/tags/{urllib.parse.quote(tag)}") or None
        except GitHubError as exc:
            if exc.status == 404:
                return None
            raise

    def list_releases(self, limit: int = 100) -> list[dict]:
        return self.request("GET", f"/releases?per_page={limit}") or []

    def ensure_release(self, tag: str, name: str, body: str, target: str) -> dict:
        """Create the release for `tag`, or update its title/notes if it already exists (a re-run of
        the same week replaces the earlier build instead of piling up duplicates)."""
        existing = self.release_by_tag(tag)
        if existing:
            self.request("PATCH", f"/releases/{existing['id']}", {"name": name, "body": body})
            return existing
        created = self.request("POST", "/releases", {"tag_name": tag, "target_commitish": target,
                                                     "name": name, "body": body})
        return created or {"id": 0, "assets": [], "upload_url": "", "html_url": self.web_url(f"/releases/tag/{tag}")}

    def upload_asset(self, release: dict, path: Path) -> None:
        for asset in release.get("assets") or []:
            if asset["name"] == path.name:   # replace an asset of the same name
                self.request("DELETE", f"/releases/assets/{asset['id']}")
        upload = (release.get("upload_url") or "").split("{", 1)[0]
        url = f"{upload}?name={urllib.parse.quote(path.name)}"
        if self.dry_run:
            self.calls.append(f"POST <uploads>/releases/{release.get('id')}/assets?name={path.name} "
                              f"({path.stat().st_size} bytes)")
            return
        self.request("POST", url, data=path.read_bytes(), content_type="application/octet-stream")

    def delete_release(self, release: dict) -> None:
        self.request("DELETE", f"/releases/{release['id']}")
        self.request("DELETE", f"/git/refs/tags/{urllib.parse.quote(release['tag_name'])}")

    # ---- pull requests / issues ----------------------------------------------------------------
    def open_pull(self, head: str, base: str, title: str, body: str) -> dict:
        owner = self.repo.split("/", 1)[0]
        found = self.request("GET", f"/pulls?state=open&head={urllib.parse.quote(f'{owner}:{head}')}")
        if found:
            self.request("PATCH", f"/pulls/{found[0]['number']}", {"title": title, "body": body})
            return found[0]
        return self.request("POST", "/pulls", {"head": head, "base": base, "title": title, "body": body})

    def open_issue(self, title: str, body: str, labels: list[str] | None = None) -> dict:
        return self.request("POST", "/issues", {"title": title, "body": body, "labels": labels or []})
