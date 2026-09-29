"""Documentation checks run without importing the app or accessing a database."""

import tempfile
import unittest
from pathlib import Path

from tools.checks.check_docs import validate


class PublicDocsChecks(unittest.TestCase):
    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        (self.root / "docs").mkdir()
        (self.root / "docs/public-docs.txt").write_text(
            "docs/public-docs.txt\ndocs/README.md\n", encoding="utf-8"
        )
        (self.root / "docs/README.md").write_text("# Docs\n", encoding="utf-8")

    def test_inventory_requires_review_for_new_documents(self):
        (self.root / "docs/scratch.md").write_text("draft", encoding="utf-8")
        self.assertIn(
            "Unreviewed public document: docs/scratch.md", validate(self.root, set())
        )

    def test_published_guide_cannot_depend_on_local_archive(self):
        (self.root / ".local").mkdir()
        (self.root / ".local/notes.md").write_text("local", encoding="utf-8")
        (self.root / "docs/README.md").write_text(
            "[notes](../.local/notes.md)", encoding="utf-8"
        )
        self.assertTrue(
            any("links local material" in error for error in validate(self.root, set()))
        )

    def test_force_added_local_material_is_rejected(self):
        (self.root / ".tmp").mkdir()
        (self.root / ".tmp/report.md").write_text("report", encoding="utf-8")
        self.assertIn(
            "Local working material is tracked: .tmp/report.md",
            validate(self.root, {".tmp/report.md"}),
        )

    def test_links_work_in_a_standalone_checkout(self):
        (self.root / "docs/README.md").write_text(
            "[missing](missing.md)\n[neighbor](../../other-project/README.md)\n",
            encoding="utf-8",
        )
        errors = validate(self.root, set())
        self.assertTrue(any("missing link target" in error for error in errors))
        self.assertTrue(
            any("leaves standalone repository" in error for error in errors)
        )

    def test_external_links_and_fenced_examples_do_not_require_local_files(self):
        (self.root / "docs/README.md").write_text(
            "[index](README.md)\n[external](https://example.com/guide)\n"
            "```md\n[example](not-a-real-file.md)\n```\n",
            encoding="utf-8",
        )
        self.assertEqual(validate(self.root, set()), [])


if __name__ == "__main__":
    unittest.main()
