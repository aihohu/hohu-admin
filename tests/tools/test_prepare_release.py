"""Release metadata regressions; no application services or network required."""

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TOOL = ROOT / "tools/ops/prepare_release.py"


class PrepareReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "pyproject.toml").write_text(
            '[project]\nname = "hohu-admin"\nversion = "0.1.5"\n', encoding="utf-8"
        )
        (self.root / "uv.lock").write_text(
            'version = 1\n[[package]]\nname = "hohu-admin"\nversion = "0.1.5"\n'
            '[package.source]\neditable = "."\n'
            '[[package]]\nname = "other"\nversion = "9.0.0"\n',
            encoding="utf-8",
        )
        self.changelog = self.root / "CHANGELOG.md"
        self.changelog.write_text(
            "# Changelog\n\n## [v0.1.5]\n\n### Features\n\n- 新功能。\n"
            "\n## [v0.1.4] (2026-07-03)\n\n- Old.\n",
            encoding="utf-8",
        )
        self.notes = self.root / "output/notes.md"

    def run_tool(self, tag="v0.1.5"):
        return subprocess.run(
            [
                sys.executable,
                "-B",
                str(TOOL),
                "--root",
                str(self.root),
                "--tag",
                tag,
                "--notes-file",
                str(self.notes),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONUTF8": "1"},
            check=False,
        )

    def test_extracts_only_current_release_and_preserves_unicode(self):
        result = self.run_tool()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.notes.read_text(encoding="utf-8"), "### Features\n\n- 新功能。\n"
        )

    def test_rejects_invalid_or_prerelease_tags(self):
        for tag in (
            "0.1.5",
            "v01.1.5",
            "v0.1",
            "v0.1.5rc1",
            "v0.1.5-beta.1",
            "v0.1.5+build",
            "v0.1.5\n",
        ):
            with self.subTest(tag=tag):
                self.assertEqual(self.run_tool(tag).returncode, 1)
                self.assertFalse(self.notes.exists())

    def test_rejects_tag_package_mismatch(self):
        self.assertEqual(self.run_tool("v0.1.6").returncode, 1)
        self.assertFalse(self.notes.exists())

    def test_rejects_stale_lock_version(self):
        lock = self.root / "uv.lock"
        lock.write_text(
            lock.read_text().replace('"0.1.5"', '"0.1.4"'), encoding="utf-8"
        )
        self.assertEqual(self.run_tool().returncode, 1)
        self.assertFalse(self.notes.exists())

    def test_rejects_missing_local_lock_package(self):
        lock = self.root / "uv.lock"
        lock.write_text(
            lock.read_text().replace(
                'editable = "."', 'registry = "https://example.org"'
            ),
            encoding="utf-8",
        )
        self.assertEqual(self.run_tool().returncode, 1)

    def test_rejects_missing_duplicate_empty_or_unreleased_notes(self):
        for content in (
            "## [v0.1.4]\n\nOld\n",
            "## [v0.1.5]\n\nOne\n\n## [v0.1.5]\n\nTwo\n",
            "## [v0.1.5]\n\n## [v0.1.4]\nOld\n",
            "## [v0.1.5] (Unreleased)\n\nPending\n",
            "## [v0.1.6]\n\nNew\n\n## [v0.1.5]\n\nOld\n",
        ):
            with self.subTest(content=content):
                self.changelog.write_text(content, encoding="utf-8")
                self.assertEqual(self.run_tool().returncode, 1)
                self.assertFalse(self.notes.exists())

    def test_heading_inside_fenced_example_is_not_a_release(self):
        content = "## [v0.1.5]\n\nExample:\n```md\n## [v0.1.4]\n```\n\nEnd.\n"
        self.changelog.write_text(content, encoding="utf-8")
        result = self.run_tool()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("End.", self.notes.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
