"""The vendored pptx skill (.github/skills/): how a re-vendor treats our copy, and how our code uses it."""
import contextlib
import importlib.util
import io
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers

from lib import pptx_edit as px

SKILLS = helpers.REPO / ".github" / "skills"
TEMPLATE = helpers.REPO / "templates" / "cadence-template.pptx"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


check_updates = _load("check_updates", SKILLS / "skill-updater" / "scripts" / "check_updates.py")


def tarball(files: dict[str, str]) -> bytes:
    """A gzipped tarball shaped like GitHub's /tarball/<sha>: the repository under one top folder."""
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tf:
        for name, text in {"README.md": "repo", **files}.items():
            data = text.encode()
            info = tarfile.TarInfo(f"anthropics-skills-abc123/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return out.getvalue()


class Revendor(unittest.TestCase):
    OURS = "Our license terms.\n"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dest = Path(tmp.name) / "pptx"
        (self.dest / "scripts").mkdir(parents=True)
        (self.dest / "LICENSE.txt").write_text(self.OURS, encoding="utf-8")
        (self.dest / "scripts" / "gone.py").write_text("old", encoding="utf-8")

    def revendor(self, upstream: dict[str, str]) -> None:
        blob = tarball({f"skills/pptx/{name}": text for name, text in upstream.items()})
        with mock.patch.object(check_updates, "gh_request", return_value=blob), \
                contextlib.redirect_stdout(io.StringIO()):
            check_updates.revendor("anthropics", "skills", "skills/pptx", "abc123", self.dest)

    def read(self, name: str) -> str:
        return (self.dest / name).read_text(encoding="utf-8")

    def test_upstream_files_replace_ours_but_our_license_stays(self):
        self.revendor({"SKILL.md": "new", "scripts/pack.py": "new", "LICENSE.txt": "Upstream's new terms.\n"})
        self.assertEqual((self.read("SKILL.md"), self.read("scripts/pack.py")), ("new", "new"))
        self.assertFalse((self.dest / "scripts" / "gone.py").exists())
        self.assertEqual(self.read("LICENSE.txt"), self.OURS)

    def test_a_license_upstream_dropped_is_kept(self):
        self.revendor({"SKILL.md": "new"})
        self.assertEqual(self.read("LICENSE.txt"), self.OURS)

    def test_a_file_pointing_outside_the_skill_leaves_it_as_it_was(self):
        with self.assertRaises(RuntimeError):
            self.revendor({"SKILL.md": "new", "../../escape.py": "x"})
        self.assertEqual(self.read("scripts/gone.py"), "old")
        self.assertFalse((self.dest.parent / "escape.py").exists())

    def test_an_empty_upstream_path_leaves_the_skill_as_it_was(self):
        with self.assertRaises(RuntimeError):
            self.revendor({})
        self.assertEqual((self.read("LICENSE.txt"), self.read("scripts/gone.py")), (self.OURS, "old"))


class Packing(unittest.TestCase):
    """pack.py validates only when it is given an original; ours always gives it one (lib/pptx_edit.py)."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.out = self.tmp / "deck.pptx"

    def test_a_broken_deck_is_not_written(self):
        unpacked = self.tmp / "unpacked"
        px.unpack(TEMPLATE, unpacked)
        slide = unpacked / helpers.slide_member(TEMPLATE, "terraform-enterprise-releases")
        xml = slide.read_text(encoding="utf-8")
        broken = re.sub(r'r:id="rId\d+"', 'r:id="rId999"', xml, count=1)   # a link to no relationship
        self.assertNotEqual(broken, xml)
        slide.write_text(broken, encoding="utf-8")
        packed = px.pack(unpacked, self.out, original=TEMPLATE)
        self.assertNotEqual(packed.returncode, 0)
        self.assertIn("rId999", packed.stdout)
        self.assertFalse(self.out.exists())

    def test_a_pack_must_name_an_original_that_exists(self):
        with self.assertRaises(TypeError):
            px.pack(self.tmp, self.out)
        with self.assertRaises(FileNotFoundError):
            px.pack(self.tmp, self.out, original=self.tmp / "missing.pptx")
        self.assertFalse(self.out.exists())

    def test_only_that_wrapper_runs_the_vendored_pack_script(self):
        code = [*helpers.SCRIPTS.rglob("*.py"), *helpers.REPO.glob("*.sh"), *helpers.REPO.glob("ci/*.sh")]
        callers = {str(f.relative_to(helpers.REPO)) for f in code if "office/pack.py" in f.read_text(encoding="utf-8")}
        self.assertEqual(callers, {"scripts/lib/pptx_edit.py"})


class WeeklyBuildCheck(unittest.TestCase):
    """build.sh, which both pipelines run, uses the skill's scripts; it checks the skill while it
    fetches, and an update upstream never fails the build. Every python call goes to a stand-in."""
    CHECK = ".github/skills/skill-updater/scripts/check_updates.py --check"

    def build(self, **env: str) -> tuple[int, list[str]]:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        shutil.copy(helpers.REPO / "build.sh", root / "build.sh")
        python = root / "python"
        python.write_text('#!/bin/sh\necho "$*" >> "$CALLS"\n'
                          'case "$*" in *check_updates.py*) exit 10 ;; esac\n', encoding="utf-8")
        python.chmod(0o755)
        calls = root / "calls"
        proc = subprocess.run(["bash", str(root / "build.sh")], capture_output=True, text=True, timeout=60,
                              env={**os.environ, "PYTHON": str(python), "CALLS": str(calls), "SKIP_FETCH": "0",
                                   "ENRICH": "0", "STRICT": "0", **env})
        return proc.returncode, calls.read_text(encoding="utf-8").splitlines()

    def test_the_skill_is_checked_while_fetching_and_an_update_does_not_fail_the_build(self):
        rc, calls = self.build()
        self.assertEqual(rc, 0)
        self.assertIn(self.CHECK, calls)
        self.assertLess(calls.index(self.CHECK), calls.index("scripts/build_deck.py"))

    def test_an_offline_rebuild_does_not_check(self):
        rc, calls = self.build(SKIP_FETCH="1")
        self.assertEqual(rc, 0)
        self.assertNotIn(self.CHECK, calls)
        self.assertIn("scripts/build_deck.py", calls)


if __name__ == "__main__":
    unittest.main()
