"""Shared plumbing for the tests: import path, fixture locations, and running the pipeline scripts
against a frozen snapshot of fetched data in a scratch directory (no network, no model)."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SNAPSHOT = FIXTURES / "snapshot-2026-10-13"
SNAPSHOT_DATE = "2026-10-13"
sys.path.insert(0, str(SCRIPTS))
# Never download or install Bob Shell from a test (lib/bob.py installs it on demand).
os.environ["BOB_SHELL_INSTALL"] = "0"
# Nor see the pipeline's real credentials: with them, the steps choose other paths than a test expects
# (the self-heal run tests a repair with its keys in the environment).
for _name in ("ICA_API_KEY", "ICA_BASE_URL", "BOB_API_KEY", "BOBSHELL_API_KEY"):
    os.environ.pop(_name, None)


def html(name: str) -> str:
    return (FIXTURES / "html" / name).read_text(encoding="utf-8")


def snapshot_env(workdir: Path) -> dict:
    """A scratch copy of the snapshot's data/ and state/, and the environment that points the
    scripts at it with "today" pinned to the snapshot's date."""
    shutil.copytree(SNAPSHOT / "data", workdir / "data")
    shutil.copytree(SNAPSHOT / "state", workdir / "state")
    return {**os.environ, "AUTO_PPTX_DATA_DIR": str(workdir / "data"),
            "AUTO_PPTX_STATE_DIR": str(workdir / "state"), "BUILD_DATE": SNAPSHOT_DATE}


def run(script: str, *args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args], env=env, cwd=str(REPO),
                          capture_output=True, text=True)


def slide_member(deck: Path, role: str) -> str:
    """The zip member name of the slide stamped with `role`."""
    with zipfile.ZipFile(deck) as z:
        for name in z.namelist():
            if name.startswith("ppt/slides/slide") and name.endswith(".xml"):
                if f'name="auto-pptx:{role}"' in z.read(name).decode("utf-8"):
                    return name
    raise AssertionError(f"no slide with role {role} in {deck}")


def rewrite(deck: Path, out: Path, member: str, edit) -> None:
    """Copy `deck` to `out`, passing the text of one zip member through `edit`."""
    with zipfile.ZipFile(deck) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            data = src.read(item.filename)
            if item.filename == member:
                changed = edit(data.decode("utf-8"))
                assert changed != data.decode("utf-8"), f"the edit did not change {member}"
                data = changed.encode("utf-8")
            dst.writestr(item, data)


def slide_text(deck: Path, role: str) -> str:
    with zipfile.ZipFile(deck) as z:
        return z.read(slide_member(deck, role)).decode("utf-8")
