"""The vendored pptx skill (.github/skills/): how a re-vendor treats our copy, and how our code uses it."""
import contextlib
import importlib.util
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers

SKILLS = helpers.REPO / ".github" / "skills"


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

    def test_an_empty_upstream_path_leaves_the_skill_as_it_was(self):
        with self.assertRaises(RuntimeError):
            self.revendor({})
        self.assertEqual((self.read("LICENSE.txt"), self.read("scripts/gone.py")), (self.OURS, "old"))


if __name__ == "__main__":
    unittest.main()
