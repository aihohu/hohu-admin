"""Regress immutable publication failures without touching a registry."""

import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.ops import sync_image_mirror as mirror

ROOT = Path(__file__).resolve().parents[2]
SOURCE = "ghcr.io/aihohu/hohu-admin"
TARGET = "registry.cn-beijing.aliyuncs.com/hohu/hohu-admin"


def index(marker="a"):
    return json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {
                    "mediaType": "application/vnd.oci.image.manifest.v1+json",
                    "size": 123,
                    "digest": "sha256:" + marker * 64,
                    "platform": {"os": "linux", "architecture": arch},
                }
                for arch in ("amd64", "arm64")
            ],
        }
    ).encode()


class ReleaseSafetyTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / ".local/tests/release-safety"
        parent.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.raw = index()
        self.digest = "sha256:" + hashlib.sha256(self.raw).hexdigest()
        self.writes = []

    def sync(self):
        return mirror.sync_image(
            SOURCE, TARGET, self.digest, ["0.1.6"], self.root, attempts=1
        )

    def test_complete_source_tag_drift_is_failure_not_skipped(self):
        def transport(args, **_kwargs):
            if args[0] == "copy":
                self.writes.append(args)
            return index("b") if args[-1] == f"docker://{SOURCE}:0.1.6" else self.raw

        with (
            patch.object(mirror, "_run", transport),
            patch.object(mirror, "probe_digest", return_value=None),
        ):
            with self.assertRaises(mirror.ImageMirrorError):
                self.sync()
        self.assertEqual(self.writes, [])

    def test_conflicting_complete_target_never_gets_overwritten(self):
        def transport(args, **_kwargs):
            if args[0] == "copy":
                self.writes.append(args)
            return index("b") if args[-1] == f"docker://{TARGET}:0.1.6" else self.raw

        with (
            patch.object(mirror, "_run", transport),
            patch.object(mirror, "probe_digest", return_value="sha256:" + "b" * 64),
        ):
            with self.assertRaises(mirror.ImageMirrorError):
                self.sync()
        self.assertEqual(self.writes, [])

    def test_workflow_stops_aliases_and_guards_build_attempt(self):
        workflow = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf8")
        self.assertNotIn("latest=true", workflow)
        self.assertNotIn("pattern={{major}}.{{minor}}", workflow)
        build = workflow.split("\n  docker:\n", 1)[1].split("\n  acr:\n", 1)[0]
        self.assertIn("release_build guard", build)
        self.assertLess(
            build.index("release_build guard"),
            build.index("docker/build-push-action@v6"),
        )
        self.assertIn("component-build-", build)

    def test_same_complete_digest_only_verifies_without_upload(self):
        def transport(args, **_kwargs):
            if args[0] == "copy" and args[-1].startswith("docker://"):
                self.writes.append(args)
            return self.raw

        with (
            patch.object(mirror, "_run", transport),
            patch.object(mirror, "probe_digest", return_value=self.digest),
        ):
            result = self.sync()
        self.assertEqual(self.writes, [])
        self.assertEqual(result["verified_tags"], ["0.1.6"])
        self.assertTrue(result["anonymous_pull"])

    def test_copy_ack_loss_reads_back_before_any_second_upload(self):
        state = {"target": None}

        def transport(args, **_kwargs):
            if args[0] == "copy" and args[-1].startswith("docker://"):
                self.writes.append(args)
                state["target"] = self.digest
                raise subprocess.TimeoutExpired(args, 1)
            return self.raw

        with (
            patch.object(mirror, "_run", transport),
            patch.object(
                mirror, "probe_digest", side_effect=lambda *_: state["target"]
            ),
        ):
            result = mirror.sync_image(
                SOURCE,
                TARGET,
                self.digest,
                ["0.1.6"],
                self.root,
                attempts=2,
                retry_delay=0,
            )
        self.assertEqual(len(self.writes), 1)
        self.assertTrue(result["anonymous_pull"])

    def test_retry_conflict_cannot_overwrite_another_complete_digest(self):
        state = {"target": None}

        def transport(args, **_kwargs):
            if args[0] == "copy" and args[-1].startswith("docker://"):
                self.writes.append(args)
                state["target"] = "sha256:" + "b" * 64
                raise subprocess.TimeoutExpired(args, 1)
            return self.raw

        with (
            patch.object(mirror, "_run", transport),
            patch.object(
                mirror, "probe_digest", side_effect=lambda *_: state["target"]
            ),
        ):
            with self.assertRaises(mirror.ImageConflictError):
                mirror.sync_image(
                    SOURCE,
                    TARGET,
                    self.digest,
                    ["0.1.6"],
                    self.root,
                    attempts=2,
                    retry_delay=0,
                )
        self.assertEqual(len(self.writes), 1)


if __name__ == "__main__":
    unittest.main()
