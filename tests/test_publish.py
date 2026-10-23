"""Publishing the verified deck as a GitHub release (scripts/publish_release.py)."""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import helpers  # noqa: F401

import publish_release
from lib import github


class Credentials(unittest.TestCase):
    """A built and verified deck of 2026-10-13, in a scratch directory, and no token anywhere."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        (root / "output").mkdir()
        deck = root / "output" / "deck-2026-10-13.pptx"
        deck.write_bytes(b"pptx")
        (root / "output" / "summary-2026-10-13.md").write_text("# This week\n", encoding="utf-8")
        reports = {"build-report.json": {"deck": str(deck), "build": {"date": "2026-10-13"}},
                   "verify-report.json": {"ok": True}}
        env = {k: v for k, v in os.environ.items() if k not in github.TOKEN_VARS}
        env.update(GITHUB_HOST="github.invalid", GITHUB_REPO="o/r")
        head = subprocess.CompletedProcess([], 0, stdout="abc123\n")
        for patch in (mock.patch.dict(os.environ, env, clear=True),
                      mock.patch.object(publish_release, "REPO_ROOT", root),
                      mock.patch.object(publish_release, "DATA_DIR", root / "data"),
                      mock.patch.object(publish_release, "read_json", reports.get),
                      mock.patch.object(publish_release.subprocess, "run", return_value=head),
                      mock.patch.object(github.urllib.request, "urlopen",
                                        side_effect=AssertionError("GitHub was contacted"))):
            patch.start()
            self.addCleanup(patch.stop)

    def publish(self, *args: str) -> tuple[int, str]:
        err = io.StringIO()
        with mock.patch.object(sys, "argv", ["publish_release.py", *args]), contextlib.redirect_stderr(err):
            rc = publish_release.main()
        return rc, err.getvalue()

    def test_a_dry_run_needs_no_token(self):
        rc, out = self.publish("--dry-run")
        self.assertEqual(rc, 0, out)
        self.assertIn("[dry-run] POST https://github.invalid/api/v3/repos/o/r/releases", out)
        self.assertIn("would attach deck-2026-10-13.pptx", out)

    def test_a_publish_or_a_check_still_does(self):
        for args in ([], ["--check"], ["--check", "--dry-run"]):
            rc, out = self.publish(*args)
            self.assertEqual(rc, 1, args)
            self.assertIn("no GitHub token", out)


if __name__ == "__main__":
    unittest.main()
