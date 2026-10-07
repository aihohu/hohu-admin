"""Exercise digest-pinned ACR copying without network or registry credentials."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TOOL = Path(__file__).resolve().parents[2] / "tools/ops/sync_image_mirror.py"
SPEC = importlib.util.spec_from_file_location("image_mirror", TOOL)
mirror = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(mirror)

SOURCE = "ghcr.io/aihohu/hohu-admin"
TARGET = "registry.cn-beijing.aliyuncs.com/hohu/hohu-admin"


def manifest(architectures=("amd64", "arm64")):
    return json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {
                    "digest": "sha256:" + str(index) * 64,
                    "platform": {"os": "linux", "architecture": architecture},
                }
                for index, architecture in enumerate(architectures, start=1)
            ],
        }
    ).encode()


RAW = manifest()
DIGEST = "sha256:" + hashlib.sha256(RAW).hexdigest()


class ImageMirrorTests(unittest.TestCase):
    def setUp(self):
        parent = TOOL.parents[2] / ".local/tests/image-mirror"
        parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.calls = []

    def transport(self, arguments, *, timeout):
        self.calls.append((arguments, timeout))
        return RAW if arguments[0] == "inspect" else b""

    def sync(self, tags=None, **options):
        return mirror.sync_image(
            SOURCE,
            TARGET,
            DIGEST,
            tags or ["test-abcdef123456-123-1"],
            self.root / "scratch",
            retry_delay=0,
            **options,
        )

    def test_test_tag_is_unique_and_never_a_release_alias(self):
        sha = "abcdef123456" + "0" * 28
        self.assertEqual(mirror.test_tag(sha, "123", "2"), "test-abcdef123456-123-2")
        self.assertNotEqual(
            mirror.test_tag(sha, "123", "1"), mirror.test_tag(sha, "123", "2")
        )
        for values in (
            ("bad", "1", "1"),
            (sha, "latest", "1"),
            (sha, "1", "1\nlatest"),
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                mirror.test_tag(*values)

    def test_target_is_official_matching_acr_repository(self):
        self.assertEqual(
            mirror.validate_target(
                SOURCE, "registry.cn-beijing.aliyuncs.com", "hohu/hohu-admin"
            ),
            TARGET,
        )
        for registry, repository in (
            ("https://registry.cn-beijing.aliyuncs.com", "hohu/hohu-admin"),
            ("attacker.invalid", "hohu/hohu-admin"),
            ("registry.cn-beijing.aliyuncs.com", "hohu/hohu-admin-web"),
            ("registry.cn-beijing.aliyuncs.com", "hohu/hohu-admin:latest"),
            ("registry.cn-beijing.aliyuncs.com", "hohu/hohu-admin\nother"),
        ):
            with (
                self.subTest(registry=registry, repository=repository),
                self.assertRaises(ValueError),
            ):
                mirror.validate_target(SOURCE, registry, repository)
        with self.assertRaises(ValueError):
            mirror.validate_target(
                "ghcr.io/fork/hohu-admin",
                "registry.cn-beijing.aliyuncs.com",
                "hohu/hohu-admin",
            )

    def test_copies_exact_digest_all_architectures_and_pulls_anonymously(self):
        with patch.object(mirror, "_run", self.transport):
            result = self.sync(tags=["0.1.0", "0.1", "latest"])
        self.assertEqual(result["digest"], DIGEST)
        self.assertEqual(result["verified_tags"], ["0.1.0", "0.1", "latest"])
        self.assertEqual(result["platforms"], ["linux/amd64", "linux/arm64"])
        self.assertTrue(result["anonymous_pull"])
        copies = [args for args, _ in self.calls if args[0] == "copy"]
        uploads = [args for args in copies if args[-1].startswith("docker://")]
        self.assertEqual(len(uploads), 3)
        for arguments in copies:
            self.assertIn("--all", arguments)
            self.assertIn("--preserve-digests", arguments)
        self.assertTrue(
            all(args[-2] == f"docker://{SOURCE}@{DIGEST}" for args in uploads)
        )
        downloads = [args for args in copies if args[-1].startswith("oci:")]
        self.assertEqual(len(downloads), 1)
        self.assertIn("--src-no-creds", downloads[0])
        self.assertEqual(downloads[0][-2], f"docker://{TARGET}@{DIGEST}")
        self.assertTrue(
            all(
                "--no-creds" in args
                for args, _ in self.calls
                if args[0] == "inspect" and args[-1].startswith(f"docker://{TARGET}")
            )
        )
        self.assertEqual(list((self.root / "scratch").iterdir()), [])

    def test_transient_copy_failure_retries_pinned_digest(self):
        failures = []

        def interrupted(arguments, *, timeout):
            if arguments[0] == "copy" and arguments[-1].startswith("docker://"):
                failures.append(arguments)
                if len(failures) == 1:
                    raise subprocess.TimeoutExpired(arguments, timeout)
            return self.transport(arguments, timeout=timeout)

        with patch.object(mirror, "_run", interrupted):
            result = self.sync(attempts=2)
        self.assertTrue(result["anonymous_pull"])
        self.assertEqual(len(failures), 2)
        self.assertEqual(failures[0][-2], failures[1][-2])

    def test_mismatched_target_never_reports_success(self):
        def mismatched(arguments, *, timeout):
            if arguments[0] == "inspect" and arguments[-1].startswith(
                f"docker://{TARGET}"
            ):
                return manifest(("amd64",))
            return self.transport(arguments, timeout=timeout)

        with (
            patch.object(mirror, "_run", mismatched),
            self.assertRaisesRegex(mirror.ImageMirrorError, "digest"),
        ):
            self.sync(attempts=2)
        self.assertEqual(sum(args[0] == "copy" for args, _ in self.calls), 2)
        self.assertEqual(list((self.root / "scratch").iterdir()), [])

    def test_source_missing_architecture_never_pushes(self):
        raw = manifest(("amd64",))
        digest = "sha256:" + hashlib.sha256(raw).hexdigest()
        with (
            patch.object(mirror, "_run", return_value=raw) as transport,
            self.assertRaisesRegex(mirror.ImageMirrorError, "arm64"),
        ):
            mirror.sync_image(
                SOURCE, TARGET, digest, ["test-1"], self.root / "scratch", attempts=1
            )
        self.assertFalse(
            any(call.args[0][0] == "copy" for call in transport.call_args_list)
        )

    def test_source_digest_mismatch_never_pushes(self):
        with (
            patch.object(
                mirror, "_run", return_value=manifest(("amd64",))
            ) as transport,
            self.assertRaisesRegex(mirror.ImageMirrorError, "digest"),
        ):
            self.sync(attempts=1)
        self.assertFalse(
            any(call.args[0][0] == "copy" for call in transport.call_args_list)
        )

    def test_source_connection_failure_is_bounded(self):
        failure = subprocess.CalledProcessError(1, ["skopeo", "inspect"])
        with (
            patch.object(mirror, "_run", side_effect=failure) as transport,
            self.assertRaisesRegex(mirror.ImageMirrorError, "3 attempts"),
        ):
            self.sync()
        self.assertEqual(transport.call_count, 3)

    def test_moved_alias_is_skipped_without_overwriting(self):
        def moved(arguments, *, timeout):
            if arguments[-1] == f"docker://{SOURCE}:latest":
                return manifest(("amd64",))
            return self.transport(arguments, timeout=timeout)

        with patch.object(mirror, "_run", moved):
            result = self.sync(tags=["0.1.0", "latest"])
        self.assertEqual(result["verified_tags"], ["0.1.0"])
        self.assertEqual(result["skipped_tags"], ["latest"])
        self.assertFalse(
            any(args[-1] == f"docker://{TARGET}:latest" for args, _ in self.calls)
        )

    def test_retry_checks_if_tag_has_moved(self):
        state = {"failed": False}

        def moved_on_retry(arguments, *, timeout):
            if arguments[0] == "copy":
                state["failed"] = True
                raise subprocess.CalledProcessError(1, arguments)
            if arguments[-1] == f"docker://{SOURCE}:latest" and state["failed"]:
                return manifest(("amd64",))
            return self.transport(arguments, timeout=timeout)

        with patch.object(mirror, "_run", moved_on_retry):
            result = self.sync(tags=["latest"], attempts=2)
        self.assertEqual(result["verified_tags"], [])
        self.assertFalse(result["anonymous_pull"])
        self.assertEqual(result["skipped_tags"], ["latest"])

    def test_anonymous_blob_pull_failure_is_not_success(self):
        def private_blobs(arguments, *, timeout):
            if arguments[0] == "copy" and arguments[-1].startswith("oci:"):
                raise subprocess.CalledProcessError(1, arguments)
            return self.transport(arguments, timeout=timeout)

        with (
            patch.object(mirror, "_run", private_blobs),
            self.assertRaisesRegex(mirror.ImageMirrorError, "anonymous"),
        ):
            self.sync(attempts=2)

    def test_transfer_and_inspect_have_separate_deadlines(self):
        with patch.object(mirror, "_run", self.transport):
            self.sync(command_timeout=30, copy_timeout=600)
        self.assertTrue(
            all(
                timeout == (600 if args[0] == "copy" else 30)
                for args, timeout in self.calls
            )
        )

    def test_invalid_arguments_fail_before_transport(self):
        for options in ({"attempts": 0}, {"command_timeout": 0}, {"copy_timeout": 0}):
            with (
                self.subTest(options=options),
                patch.object(mirror, "_run") as transport,
                self.assertRaises(ValueError),
            ):
                self.sync(**options)
            transport.assert_not_called()
        for digest, tags in (
            ("latest", ["test-1"]),
            (DIGEST, []),
            (DIGEST, ["x\nlatest"]),
            (DIGEST, ["--all"]),
        ):
            with (
                self.subTest(digest=digest, tags=tags),
                patch.object(mirror, "_run") as transport,
                self.assertRaises(ValueError),
            ):
                mirror.sync_image(SOURCE, TARGET, digest, tags, self.root / "scratch")
            transport.assert_not_called()

    def test_tags_file_cannot_redirect_repository(self):
        self.assertEqual(
            mirror.parse_tags(SOURCE, f"{SOURCE}:0.1.0\n{SOURCE}:latest\n"),
            ["0.1.0", "latest"],
        )
        for value in (f"{SOURCE}:latest\nother/repo:latest", "latest", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                mirror.parse_tags(SOURCE, value)

    def test_timeout_preserves_transport_diagnostic(self):
        for diagnostic in (b"connection reset", "connection reset"):
            error = subprocess.TimeoutExpired(["skopeo", "copy"], 10, stderr=diagnostic)
            with (
                patch.object(mirror.subprocess, "run", side_effect=error),
                self.assertLogs(mirror.logger, "ERROR") as logs,
                self.assertRaises(subprocess.TimeoutExpired),
            ):
                mirror._run(["copy", "unused"], timeout=10)
            self.assertIn("connection reset", "\n".join(logs.output))

    def test_raw_output_is_not_stripped_or_text_decoded(self):
        raw = b'{"schemaVersion":2} \n'
        with patch.object(
            mirror.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, raw, b""),
        ) as transport:
            self.assertEqual(mirror._run(["inspect", "unused"], timeout=10), raw)
        self.assertNotIn("shell", transport.call_args.kwargs)
        self.assertNotIn("text", transport.call_args.kwargs)

    def test_cli_writes_verified_report_and_fails_on_copy_error(self):
        tags_file = self.root / "tags.txt"
        tags_file.write_text(f"{SOURCE}:test-1\n", encoding="utf-8")
        report = self.root / "report.json"
        arguments = [
            "mirror",
            "sync",
            "--source",
            SOURCE,
            "--registry",
            "registry.cn-beijing.aliyuncs.com",
            "--repository",
            "hohu/hohu-admin",
            "--digest",
            DIGEST,
            "--tags-file",
            str(tags_file),
            "--work-dir",
            str(self.root / "scratch"),
            "--report",
            str(report),
            "--attempts",
            "1",
        ]
        with (
            patch.object(sys, "argv", arguments),
            patch.object(mirror, "_run", self.transport),
        ):
            self.assertEqual(mirror.main(), 0)
        self.assertTrue(
            json.loads(report.read_text(encoding="utf-8"))["anonymous_pull"]
        )
        report.unlink()
        with (
            patch.object(sys, "argv", arguments),
            patch.object(
                mirror, "_run", side_effect=FileNotFoundError("skopeo missing")
            ),
        ):
            self.assertEqual(mirror.main(), 1)
        self.assertFalse(report.exists())


if __name__ == "__main__":
    unittest.main()
