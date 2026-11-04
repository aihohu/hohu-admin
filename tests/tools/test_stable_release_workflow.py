"""Verify source selection cannot cross the protected controller boundary."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
CI = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")


def job(name):
    return re.search(rf"^  {name}:\n(.*?)(?=^  [\w-]+:|\Z)", WORKFLOW, re.M | re.S)[1]


class StableWorkflowTests(unittest.TestCase):
    def test_rehearsal_has_no_publication_permissions_or_environment(self):
        rehearsal = (ROOT / ".github/workflows/stable-verify.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("contents: read", rehearsal)
        self.assertNotIn("contents: write", rehearsal)
        self.assertNotIn("packages: write", rehearsal)
        self.assertNotIn("environment:", rehearsal)
        self.assertNotIn("secrets.", rehearsal)
        self.assertNotIn("docker/login-action", rehearsal)
        self.assertIn("--verify-only", rehearsal)
        self.assertIn("push: false", rehearsal)
        self.assertIn("include-hidden-files: true", rehearsal)
        self.assertIn("type=oci,dest=.local/artifacts/stable-candidate.tar", rehearsal)
        self.assertIn("source_sha: ${{ needs.prepare.outputs.source_sha }}", rehearsal)
        self.assertNotIn("tools.ops.release_tag", rehearsal)
        self.assertNotIn("tools.ops.release_build", rehearsal)
        if not ROOT.name.endswith("-web"):
            self.assertIn("--profile release", rehearsal)

    def test_selection_is_validated_before_checkout_or_tagging(self):
        prepare = job("prepare")
        self.assertIn("stable-0.1", WORKFLOW)
        self.assertIn("tools.ops.stable_release", prepare)
        self.assertLess(
            prepare.index("tools.ops.stable_release"),
            prepare.index("path: .local/release-source"),
        )
        self.assertLess(
            prepare.index("tools.ops.stable_release"),
            prepare.index("release_tag check"),
        )
        self.assertIn("ref: ${{ github.sha }}", prepare)
        self.assertIn("--root .local/release-source", prepare)
        self.assertIn("source_sha: ${{ steps.selection.outputs.source_sha }}", prepare)
        self.assertIn("provenance: ${{ steps.selection.outputs.provenance }}", prepare)

    def test_secret_bearing_tools_always_checkout_the_controller(self):
        for name in ("tag", "docker", "acr", "github-release"):
            section = job(name)
            with self.subTest(job=name):
                self.assertIn("environment: docker", section)
                self.assertIn("ref: ${{ github.sha }}", section)
                self.assertNotIn("ref: ${{ needs.docker.outputs.commit }}", section)
        for name in ("docker", "acr", "github-release"):
            self.assertIn(
                "RELEASE_SOURCE_SHA: ${{ needs.prepare.outputs.source_sha }}", job(name)
            )
            self.assertIn(
                "RELEASE_PROVENANCE: ${{ needs.prepare.outputs.provenance }}", job(name)
            )

    def test_build_and_oci_revision_use_application_source_without_tool_credentials(
        self,
    ):
        docker = job("docker")
        self.assertIn("context: .local/release-source", docker)
        self.assertIn("file: .local/release-source/Dockerfile", docker)
        self.assertIn("path: .local/release-source", docker)
        self.assertIn("ref: ${{ needs.prepare.outputs.source_sha }}", docker)
        self.assertIn(
            "org.opencontainers.image.revision=${{ needs.prepare.outputs.source_sha }}",
            docker,
        )
        self.assertIn("git -C .local/release-source rev-parse HEAD", docker)
        self.assertNotIn(".local/release-source/.local/private", WORKFLOW)
        self.assertIn("--root .local/release-source", job("github-release"))

    def test_all_reusable_ci_checkouts_are_explicitly_bound_to_selected_sha(self):
        self.assertIn("source_sha:", CI)
        refs = re.findall(r"^\s+ref: (.+)$", CI, re.M)
        self.assertTrue(refs)
        self.assertEqual(set(refs), {"${{ inputs.source_sha || github.sha }}"})
        self.assertIn("source_sha: ${{ needs.prepare.outputs.source_sha }}", job("ci"))
        self.assertNotIn("secrets:", job("ci"))
        if not ROOT.name.endswith("-web"):
            self.assertIn(
                "ref: ${{ needs.prepare.outputs.source_sha }}", job("qualification")
            )
            self.assertIn("needs: [prepare, ci]", job("qualification"))
            self.assertIn("if [[ -f tests/release/bootstrap_database.py ]]; then", CI)
            self.assertIn("HOHU_TEST_DATABASE_EPHEMERAL=true", CI)
            self.assertIn('RELEASE_BUILD_SHA="$(git rev-parse HEAD)"', CI)
            self.assertIn("python -m tests.release.bootstrap_database", CI)
            self.assertIn("HOHU_ADMIN_PASSWORD=CiSeed123456", CI)
            self.assertNotIn("bootstrap_database ||", CI)
