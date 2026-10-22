"""Verify original-build provenance and interrupted downstream publication."""

import copy
import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from tools.ops import release_build as build
from tools.ops import release_registry as registry

ROOT = Path(__file__).resolve().parents[2]
REPO = "aihohu/hohu-admin-web" if ROOT.name.endswith("-web") else "aihohu/hohu-admin"


class GitHubFixture:
    def __init__(self, expected, raw):
        self.expected = expected
        self.calls = []
        self.run = {
            "id": 123,
            "run_attempt": 1,
            "head_sha": expected["source_sha"],
            "event": "workflow_dispatch",
            "path": ".github/workflows/release.yml",
            "repository": {"full_name": REPO},
        }
        self.job = {
            "name": build.BUILD_JOB,
            "head_sha": expected["source_sha"],
            "status": "completed",
            "conclusion": "success",
            "started_at": "2026-10-22T00:00:00Z",
            "completed_at": "2026-10-22T00:05:00Z",
        }
        self.artifact = {
            "id": 999,
            "name": "component-build-123-1",
            "expired": False,
            "created_at": "2026-10-22T00:04:59Z",
            "workflow_run": {"id": 123, "head_sha": expected["source_sha"]},
        }
        self.reset_archive(raw)
        self.release = None
        self.assets = {}
        self.asset_content = {}
        self.fail = None
        self.uploads = []

    def reset_archive(self, raw, name=build.RECORD_NAME):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr(name, raw)
        self.archive = buffer.getvalue()
        self.artifact["digest"] = registry.digest(self.archive)

    def request(self, method, path, payload=None, *, missing_ok=False):
        self.calls.append((method, path, payload))
        if path == "actions/runs/123/attempts/1":
            return copy.deepcopy(self.run)
        if path.endswith("/jobs?per_page=100"):
            return {"total_count": 1, "jobs": [copy.deepcopy(self.job)]}
        if path.endswith("/artifacts?per_page=100"):
            return {"total_count": 1, "artifacts": [copy.deepcopy(self.artifact)]}
        if path.startswith("git/ref/tags/"):
            return {"object": {"type": "tag", "sha": "tag-object"}}
        if path == "git/tags/tag-object":
            e = self.expected
            return {
                "tag": e["tag"],
                "object": {"type": "commit", "sha": e["source_sha"]},
                "message": f"Release {e['tag']}\n\nGitHub Actions run: https://github.com/{REPO}/actions/runs/{e['run_id']}\n",
            }
        if path.startswith("releases/tags/"):
            assert missing_ok
            if self.fail == "read":
                raise ValueError("GitHub authorization unknown")
            return copy.deepcopy(self.release)
        if path == "releases" and method == "POST":
            self.release = {"id": 42, **payload}
            if self.fail == "create_ack":
                raise ValueError("Lost create response")
            return copy.deepcopy(self.release)
        if path.endswith("/assets?per_page=100"):
            return list(self.assets.values())
        if path == "releases/42" and method == "PATCH":
            self.release.update(payload)
            if self.fail == "publish_ack":
                raise ValueError("Lost publication response")
            return copy.deepcopy(self.release)
        raise AssertionError((method, path))

    def download(self, path):
        if path == "actions/artifacts/999/zip":
            return self.archive
        return self.asset_content[int(path.rsplit("/", 1)[1])]

    def upload(self, tag, path):
        assert tag == self.expected["tag"]
        self.uploads.append(path.name)
        number = 100 + len(self.assets)
        self.assets[path.name] = {"id": number, "name": path.name}
        self.asset_content[number] = path.read_bytes()
        if self.fail == "upload_ack":
            raise ValueError("Lost upload response")


class BuildEvidenceTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / ".local/tests/release-build"
        parent.mkdir(parents=True, exist_ok=True)
        temp = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.expected = build.identity(REPO, "a" * 40, "v0.1.6", "123", None)
        self.children = {
            arch: json.dumps({"schemaVersion": 2, "architecture": arch}).encode()
            for arch in ("amd64", "arm64")
        }
        self.raw_index = json.dumps(
            {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "size": len(raw),
                        "digest": registry.digest(raw),
                        "platform": {"os": "linux", "architecture": arch},
                    }
                    for arch, raw in self.children.items()
                ],
            }
        ).encode()
        self.digest = registry.digest(self.raw_index)
        self.precheck = build.preflight(
            self.expected, exclusive_writer="true", probe=lambda *_: None
        )
        self.record = build.freeze_build(
            self.expected, self.digest, self.precheck, inspect=self.inspect
        )
        self.raw = build.canonical(self.record)
        self.api = GitHubFixture(self.expected, self.raw)
        self.record_path = self.root / build.RECORD_NAME
        self.record_path.write_bytes(self.raw)
        self.evidence_path = self.root / "component-delivery.json"
        self.evidence_path.write_bytes(b'{"verified":true}\n')

    def inspect(self, reference, _timeout, **_kwargs):
        for child in self.children.values():
            if reference.endswith("@" + registry.digest(child)):
                return child
        return self.raw_index

    def publish(self, notes="original notes"):
        build.publish(
            self.api, self.expected, self.record_path, self.evidence_path, notes
        )

    def test_formal_rerun_fails_before_any_build(self):
        build.guard("release", "1")
        build.guard("test", "2")
        for value in ("2", "50", "0", "", "1\n"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                build.guard("release", value)

    def test_recovery_rechecks_default_branch_and_writer_policy(self):
        env = {
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_SHA": "a" * 40,
            "GITHUB_RUN_ID": "123",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main",
            "DEFAULT_BRANCH": "main",
            "RELEASE_TAG": "v0.1.6",
            "RELEASE_EXCLUSIVE_WRITER_CONFIRMED": "true",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(build.environment_identity(), self.expected)
        for key, value in (
            ("RELEASE_EXCLUSIVE_WRITER_CONFIRMED", "false"),
            ("GITHUB_REF", "refs/heads/release/0.1.6"),
            ("GITHUB_EVENT_NAME", "pull_request"),
            ("ACR_MIRROR_ENABLED", "true"),
        ):
            with (
                self.subTest(key=key),
                patch.dict(os.environ, {**env, key: value}, clear=True),
                self.assertRaises(ValueError),
            ):
                build.environment_identity()

    def test_ambiguous_or_incomplete_original_inventory_rejected(self):
        original = self.api.request
        for resource in ("jobs", "artifacts"):
            for count, duplicate in ((2, False), (2, True), (0, False)):

                def inventory(
                    method,
                    path,
                    *args,
                    resource=resource,
                    count=count,
                    duplicate=duplicate,
                    **kwargs,
                ):
                    response = original(method, path, *args, **kwargs)
                    if path.endswith(f"/{resource}?per_page=100"):
                        response["total_count"] = count
                        if duplicate:
                            response[resource] *= 2
                        if count == 0:
                            response[resource] = []
                    return response

                with (
                    self.subTest(resource=resource, count=count, duplicate=duplicate),
                    patch.object(self.api, "request", side_effect=inventory),
                    self.assertRaises(ValueError),
                ):
                    build.restore(self.api, self.expected)

    def test_preflight_requires_exclusive_writer_and_authorized_absence(self):
        for exclusive in ("", "false", "TRUE"):
            with self.subTest(exclusive=exclusive), self.assertRaises(ValueError):
                build.preflight(
                    self.expected,
                    exclusive_writer=exclusive,
                    probe=lambda *_: self.fail("Must not query"),
                )
        with self.assertRaises(ValueError):
            build.preflight(
                self.expected, exclusive_writer="true", probe=lambda *_: self.digest
            )

        def unknown(*_):
            raise registry.RegistryError("Unknown")

        with self.assertRaises(registry.RegistryError):
            build.preflight(self.expected, exclusive_writer="true", probe=unknown)

    def test_all_enabled_targets_checked_before_build(self):
        target = "registry.cn-beijing.aliyuncs.com/hohu/" + REPO.split("/")[1]
        expected = build.identity(REPO, "a" * 40, "v0.1.6", "123", target)
        seen = []

        def probe(image, tag):
            seen.append((image, tag))
            return self.digest if image == target else None

        with self.assertRaises(ValueError):
            build.preflight(expected, exclusive_writer="true", probe=probe)
        self.assertEqual(seen, [(expected["source_image"], "0.1.6"), (target, "0.1.6")])

    def test_same_original_success_restores_exact_bytes_and_platforms(self):
        raw, record = build.restore(self.api, self.expected)
        self.assertEqual(raw, self.raw)
        self.assertEqual(record, self.record)
        build.verify_source(record, inspect=self.inspect)

    def test_artifact_redirect_and_release_asset_use_distinct_accept_headers(self):
        api = build.DeliveryGitHub(REPO, "unused-test-token")
        with patch.object(build.subprocess, "run") as run:
            run.return_value.stdout = b"stored bytes"
            self.assertEqual(api.download("actions/artifacts/999/zip"), b"stored bytes")
            self.assertIn("Accept: application/vnd.github+json", run.call_args.args[0])
            api.download("releases/assets/101")
            self.assertIn("Accept: application/octet-stream", run.call_args.args[0])

    def test_failed_cancelled_and_ambiguous_original_build_rejected(self):
        for state in ("failure", "cancelled", "skipped", None):
            with self.subTest(state=state), self.assertRaises(ValueError):
                self.api.job["conclusion"] = state
                build.restore(self.api, self.expected)

    def test_wrong_run_source_event_and_workflow_are_rejected(self):
        for field, value in (
            ("id", 124),
            ("run_attempt", 2),
            ("head_sha", "b" * 40),
            ("event", "push"),
            ("path", ".github/workflows/other.yml"),
            ("repository", {"full_name": "fork/repo"}),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                api = GitHubFixture(self.expected, self.raw)
                api.run[field] = value
                build.restore(api, self.expected)

    def test_expired_missing_copied_or_late_artifact_rejected(self):
        for field, value in (
            ("expired", True),
            ("name", "component-build-123-2"),
            ("workflow_run", {"id": 124, "head_sha": "a" * 40}),
            ("digest", "sha256:" + "b" * 64),
            ("created_at", "2026-10-22T01:00:00Z"),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                api = GitHubFixture(self.expected, self.raw)
                api.artifact[field] = value
                build.restore(api, self.expected)

    def test_untrusted_archive_path_and_tampered_bytes_rejected(self):
        self.api.reset_archive(self.raw, "../component-build.json")
        with self.assertRaises(ValueError):
            build.restore(self.api, self.expected)
        self.api.reset_archive(self.raw)
        self.api.archive += b"tampered"
        with self.assertRaises(ValueError):
            build.restore(self.api, self.expected)

    def test_record_rejects_changed_identity_and_missing_evidence(self):
        for key, value in (
            ("repository", "fork/repo"),
            ("source_sha", "b" * 40),
            ("tag", "v0.1.7"),
            ("run_id", "124"),
            ("build_attempt", 2),
            ("build_attempt", True),
            ("source_image", "ghcr.io/fork/repo"),
            ("mirror_image", "registry.cn-beijing.aliyuncs.com/other/hohu-admin"),
            ("platforms", {}),
            ("preflight", {}),
            ("index_digest", "latest"),
            ("extra", True),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                value_record = {**self.record, key: value}
                build.validate_record(build.canonical(value_record), self.expected)
        with self.assertRaises(ValueError):
            build.validate_record(
                self.raw.replace(
                    b'"build_attempt": 1,', b'"build_attempt": 1, "build_attempt": 1,'
                ),
                self.expected,
            )

    def test_preflight_booleans_are_not_numeric_flags(self):
        changed = copy.deepcopy(self.record)
        changed["preflight"]["exclusive_writer_confirmed"] = 1
        with self.assertRaises(ValueError):
            build.validate_record(build.canonical(changed), self.expected)

    def test_live_source_tag_and_child_manifests_cannot_drift(self):
        for suffix in (
            ":0.1.6",
            "@" + self.digest,
            "@" + registry.digest(self.children["amd64"]),
        ):
            with self.subTest(suffix=suffix), self.assertRaises(ValueError):
                build.verify_source(
                    self.record,
                    inspect=lambda ref, timeout, ending=suffix: (
                        b"{}" if ref.endswith(ending) else self.inspect(ref, timeout)
                    ),
                )

    def test_incomplete_or_duplicate_platforms_rejected(self):
        for entries in ([], [json.loads(self.raw_index)["manifests"][0]] * 2):
            raw = json.dumps(
                {**json.loads(self.raw_index), "manifests": entries}
            ).encode()
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                build.index_platforms(raw, registry.digest(raw))

    def test_delivery_requires_full_anonymous_pull(self):
        pulled = []
        result = build.delivery_evidence(
            self.record,
            self.raw,
            self.root,
            inspect=self.inspect,
            probe=lambda *_: self.digest,
            pull=lambda *args: pulled.append(args),
        )
        self.assertTrue(result["anonymous_pull"])
        self.assertEqual(pulled[0][:2], (self.expected["source_image"], self.digest))

        def failed(*_args):
            raise ValueError("Anonymous layer unavailable")

        with self.assertRaises(ValueError):
            build.delivery_evidence(
                self.record,
                self.raw,
                self.root,
                inspect=self.inspect,
                probe=lambda *_: self.digest,
                pull=failed,
            )

    def test_publish_preserves_original_evidence_and_existing_notes(self):
        self.publish()
        self.assertFalse(self.api.release["draft"])
        original_body = self.api.release["body"]
        self.assertIn("component delivery only", original_body)
        self.assertIn(
            "does not certify a Backend/Web product combination", original_body
        )
        self.assertEqual(
            self.api.uploads, [build.RECORD_NAME, "component-delivery.json"]
        )
        self.publish("new notes must not overwrite")
        self.assertEqual(self.api.release["body"], original_body)
        self.assertEqual(len(self.api.uploads), 2)

    def test_draft_creation_upload_and_publication_ack_loss_resume_by_readback(self):
        for stage in ("create_ack", "upload_ack", "publish_ack"):
            with self.subTest(stage=stage):
                self.api = GitHubFixture(self.expected, self.raw)
                self.api.fail = stage
                with self.assertRaises(ValueError):
                    self.publish()
                self.api.fail = None
                self.publish()
                self.assertFalse(self.api.release["draft"])
                self.assertEqual(
                    len([c for c in self.api.calls if c[:2] == ("POST", "releases")]), 1
                )
                self.assertEqual(self.api.uploads.count(build.RECORD_NAME), 1)

    def test_conflicting_asset_and_unknown_release_query_never_publish(self):
        self.api.fail = "read"
        with self.assertRaises(ValueError):
            self.publish()
        self.assertFalse(
            any(method in {"POST", "PATCH"} for method, _, _ in self.api.calls)
        )
        self.api.fail = None
        self.api.release = {"id": 42, "tag_name": "v0.1.6", "draft": True}
        self.api.assets[build.RECORD_NAME] = {"id": 101, "name": build.RECORD_NAME}
        self.api.asset_content[101] = b"other original evidence"
        with self.assertRaises(ValueError):
            self.publish()
        self.assertTrue(self.api.release["draft"])
        self.assertEqual(self.api.uploads, [])

    def test_moved_git_tag_stops_before_release_or_asset_writes(self):
        self.api.expected = {**self.expected, "source_sha": "b" * 40}
        with self.assertRaises(ValueError):
            self.publish()
        self.assertFalse(
            any(method in {"POST", "PATCH"} for method, _, _ in self.api.calls)
        )
        self.assertEqual(self.api.uploads, [])


if __name__ == "__main__":
    unittest.main()
