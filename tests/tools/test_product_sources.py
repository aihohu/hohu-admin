"""Verify source provenance without treating fixtures as real release evidence."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.ops import product_release_sources as sources
from tools.ops import release_build as build
from tools.ops import release_registry as registry

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/product_release/valid.json"


def raw(value):
    return json.dumps(value).encode()


class World(sources.Sources):
    def __init__(self):
        super().__init__()
        self.doc = json.loads(FIXTURE.read_bytes())
        self.responses = {}
        self.files = {}
        self.assets_data = {}
        self.manifests = {}
        self.downloads = []
        self.pulls = []
        self.calls = []
        for key, c in self.doc["components"].items():
            repo, sha, tag = c["repository"], c["commit"], c["tag"]
            run_id = c["build_run_url"].rsplit("/", 1)[1]
            image = c["images"][0]
            descriptors = []
            for p in image["platforms"]:
                child = raw(
                    {
                        "schemaVersion": 2,
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "config": {
                            "digest": registry.digest((key + p["platform"]).encode())
                        },
                        "layers": [],
                    }
                )
                p["manifest_digest"] = registry.digest(child)
                self.manifests[image["repository"] + "@" + p["manifest_digest"]] = child
                os_name, arch = p["platform"].split("/")
                descriptors.append(
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "size": len(child),
                        "digest": p["manifest_digest"],
                        "platform": {"os": os_name, "architecture": arch},
                    }
                )
            index = raw(
                {
                    "schemaVersion": 2,
                    "mediaType": "application/vnd.oci.image.index.v1+json",
                    "manifests": descriptors,
                }
            )
            image["index_digest"] = registry.digest(index)
            for ref in ("@" + image["index_digest"], ":0.1.6"):
                self.manifests[image["repository"] + ref] = index
            expected = build.identity(repo, sha, tag, run_id, None)
            record = {
                **expected,
                "index_digest": image["index_digest"],
                "platforms": {
                    p["platform"]: p["manifest_digest"] for p in image["platforms"]
                },
                "preflight": build.preflight(
                    expected, exclusive_writer="true", probe=lambda *_: None
                ),
            }
            record_raw = build.canonical(record)
            delivery = {
                "schema_version": 1,
                "build_record_sha256": registry.digest(record_raw),
                "repository": repo,
                "source_sha": sha,
                "tag": tag,
                "index_digest": image["index_digest"],
                "platforms": record["platforms"],
                "verified_images": [image["repository"]],
                "anonymous_pull": True,
            }
            self.assets_data[repo, "component-build.json"] = record_raw
            self.assets_data[repo, "component-delivery.json"] = build.canonical(
                delivery
            )
            self.responses[repo, f"releases/tags/{tag}"] = {
                "id": int(run_id),
                "tag_name": tag,
                "draft": False,
                "prerelease": False,
                "body": "Fix release.",
            }
            tag_sha = "d" * 40
            self.responses[repo, f"git/ref/tags/{tag}"] = {
                "object": {"type": "tag", "sha": tag_sha}
            }
            self.responses[repo, f"git/tags/{tag_sha}"] = {
                "tag": tag,
                "object": {"type": "commit", "sha": sha},
                "message": f"Release {tag}\n\nGitHub Actions run: {c['build_run_url']}\n",
            }
            self.responses[repo, f"actions/runs/{run_id}/attempts/1"] = {
                "id": int(run_id),
                "run_attempt": 1,
                "head_sha": sha,
                "head_branch": "main",
                "event": "workflow_dispatch",
                "path": ".github/workflows/release.yml",
                "repository": {"full_name": repo},
                "created_at": "2026-10-22T01:00:00Z",
            }
            jobs = [
                {
                    "name": build.BUILD_JOB,
                    "head_sha": sha,
                    "status": "completed",
                    "conclusion": "success",
                    "completed_at": "2026-10-22T01:10:00Z",
                }
            ]
            for name in (
                ["Hosted release qualification (release profile)"]
                if key == "backend"
                else ["Verify release source / Check and build frontend"]
            ):
                jobs.append({**jobs[0], "name": name})
            self.responses[
                repo, f"actions/runs/{run_id}/attempts/1/jobs?per_page=100"
            ] = {"total_count": len(jobs), "jobs": jobs}
            self.files[repo, sha, "CHANGELOG.md"] = b"## [v0.1.6]\n\nFix release.\n"
            if key == "backend":
                self.files[repo, sha, "pyproject.toml"] = (
                    b'[project]\nname="hohu-admin"\nversion="0.1.6"\n'
                )
                self.files[repo, sha, "uv.lock"] = (
                    b'[[package]]\nname="hohu-admin"\nversion="0.1.6"\n[package.source]\neditable="."\n'
                )
                self.responses[repo, f"git/trees/{sha}?recursive=1"] = {
                    "truncated": False,
                    "tree": [
                        {"type": "blob", "path": "alembic/versions/one.py"},
                        {"type": "blob", "path": "alembic/versions/two.py"},
                    ],
                }
                self.files[repo, sha, "alembic/versions/one.py"] = (
                    b'revision = "rev1"\ndown_revision = None\n'
                )
                self.files[repo, sha, "alembic/versions/two.py"] = (
                    b'revision: str = "rev2"\ndown_revision = "rev1"\n'
                )
            else:
                self.files[repo, sha, "package.json"] = b'{"version":"0.1.6"}'
                self.files[repo, sha, "pnpm-lock.yaml"] = (
                    b"lockfileVersion: '9.0'\nimporters:\n  .:\n    dependencies: {}\n"
                )
        for e in self.doc["evidence"]:
            for key in ("backend", "web"):
                if e[key + "_digest"] is not None:
                    e[key + "_digest"] = self.doc["components"][key]["images"][0][
                        "index_digest"
                    ]
            body = raw(
                {
                    "tools": ["synthetic-test"],
                    "commands": ["not executed"],
                    "observation": "fixture only",
                    "scope": e["kind"],
                }
            )
            e["sha256"] = registry.digest(body)
            self.assets_data["aihohu/hohu-admin", e["url"].rsplit("/", 1)[1]] = body
        self.files[
            "aihohu/hohu-admin-docs", "c" * 40, "docs/guide/reference/versions.md"
        ] = b"Synthetic documentation."

    def request(self, repo, path):
        self.calls.append((repo, path))
        return copy.deepcopy(self.responses[repo, path])

    def content(self, repo, sha, path):
        self.calls.append((repo, sha, path))
        return self.files[repo, sha, path]

    def asset(self, repo, release, name):
        self.downloads.append((repo, release["id"], name))
        return self.assets_data[repo, name]

    def inspect(self, reference):
        return self.manifests[reference]

    def pull(self, image, digest, work_dir):
        self.pulls.append((image, digest, work_dir))


class SourcesTests(unittest.TestCase):
    def test_tag_drift_during_pull_is_rejected(self):
        world = World()

        def drift(image, _digest, _directory):
            world.manifests[image + ":0.1.6"] = b"{}"

        with (
            patch.object(world, "pull", side_effect=drift),
            self.assertRaises(ValueError),
        ):
            self.verify(world)

    def test_asset_transport_inventory_and_hash(self):
        api = sources.Sources()
        payload = b"persistent report bytes"
        asset = {
            "id": 2,
            "name": "report.json",
            "state": "uploaded",
            "size": len(payload),
            "digest": registry.digest(payload),
        }
        with patch.object(api, "api_bytes", return_value=payload) as fetch:
            self.assertEqual(api.download_asset("aihohu/hohu-admin", asset), payload)
            self.assertEqual(
                fetch.call_args.kwargs["accept"], "application/octet-stream"
            )
            for field, value in (
                ("id", True),
                ("size", len(payload) + 1),
                ("state", "starter"),
                ("digest", "sha256:" + "0" * 64),
            ):
                with self.subTest(field=field), self.assertRaises(ValueError):
                    api.download_asset("aihohu/hohu-admin", {**asset, field: value})
        for inventory in ([asset] * 100, [asset, asset], [], {"message": "failure"}):
            with (
                patch.object(api, "request", return_value=inventory),
                self.assertRaises(ValueError),
            ):
                api.asset("aihohu/hohu-admin", {"id": 1}, "report.json")

    def test_attestation_and_platform_mapping_are_not_support_proof(self):
        world = World()
        component = world.doc["components"]["backend"]
        primary = component["images"][0]
        index = json.loads(
            world.manifests[primary["repository"] + "@" + primary["index_digest"]]
        )
        index["manifests"][0]["platform"] = {"os": "unknown", "architecture": "unknown"}
        with self.assertRaises(ValueError):
            build.index_platforms(raw(index), registry.digest(raw(index)))

    def verify(self, world):
        with tempfile.TemporaryDirectory() as directory:
            world.verify(world.doc, Path(directory))

    def test_complete_source_verification(self):
        world = World()
        self.verify(world)
        self.assertEqual(len(world.pulls), 2)

    def test_source_mutations_fail_closed(self):
        for scenario in (
            "tag_sha",
            "tag_run",
            "wrong_branch",
            "wrong_workflow",
            "failed_build",
            "missing_qualification",
            "jobs_truncated",
            "record_sha",
            "delivery_hash",
            "version",
            "lock",
            "notes",
            "migration",
            "tree_truncated",
            "report_hash",
            "source_drift",
            "child_corrupt",
            "future_build",
            "draft_release",
        ):
            w = World()
            repo = "aihohu/hohu-admin"
            sha = "a" * 40
            tag = w.responses[repo, "git/tags/" + "d" * 40]
            run = w.responses[repo, "actions/runs/1/attempts/1"]
            jobs = w.responses[repo, "actions/runs/1/attempts/1/jobs?per_page=100"]
            if scenario == "tag_sha":
                tag["object"]["sha"] = "b" * 40
            elif scenario == "tag_run":
                tag["message"] = "Release v0.1.6"
            elif scenario == "wrong_branch":
                run["head_branch"] = "release/0.1.6"
            elif scenario == "wrong_workflow":
                run["path"] = ".github/workflows/ci.yml"
            elif scenario == "failed_build":
                jobs["jobs"][0]["conclusion"] = "failure"
            elif scenario == "missing_qualification":
                jobs["jobs"].pop()
                jobs["total_count"] -= 1
            elif scenario == "jobs_truncated":
                jobs["total_count"] += 1
            elif scenario == "record_sha":
                record = json.loads(w.assets_data[repo, "component-build.json"])
                record["source_sha"] = "b" * 40
                w.assets_data[repo, "component-build.json"] = raw(record)
            elif scenario == "delivery_hash":
                evidence = json.loads(w.assets_data[repo, "component-delivery.json"])
                evidence["build_record_sha256"] = "sha256:" + "f" * 64
                w.assets_data[repo, "component-delivery.json"] = raw(evidence)
            elif scenario == "version":
                w.files[repo, sha, "pyproject.toml"] = (
                    b'[project]\nname="hohu-admin"\nversion="0.1.5"'
                )
            elif scenario == "lock":
                w.files[repo, sha, "uv.lock"] = (
                    b'[[package]]\nname="hohu-admin"\nversion="0.1.5"'
                )
            elif scenario == "notes":
                w.files[repo, sha, "CHANGELOG.md"] = b"## [v0.1.5]\nOld."
            elif scenario == "migration":
                w.files[repo, sha, "alembic/versions/two.py"] = (
                    b'revision = "rev2"\ndown_revision = "missing"'
                )
            elif scenario == "tree_truncated":
                w.responses[repo, f"git/trees/{sha}?recursive=1"]["truncated"] = True
            elif scenario == "report_hash":
                w.doc["evidence"][0]["sha256"] = "sha256:" + "f" * 64
            elif scenario == "source_drift":
                w.manifests["ghcr.io/aihohu/hohu-admin:0.1.6"] = b"{}"
            elif scenario == "child_corrupt":
                p = w.doc["components"]["backend"]["images"][0]["platforms"][0]
                w.manifests["ghcr.io/aihohu/hohu-admin@" + p["manifest_digest"]] = b"{}"
            elif scenario == "future_build":
                jobs["jobs"][0]["completed_at"] = "2026-10-24T00:00:00Z"
            else:
                w.responses[repo, "releases/tags/v0.1.6"]["draft"] = True
            with self.subTest(scenario=scenario), self.assertRaises(ValueError):
                self.verify(w)

    def test_unapproved_mirror_rejected_before_network(self):
        w = World()
        image = copy.deepcopy(w.doc["components"]["backend"]["images"][0])
        image.update(
            role="mirror", repository="registry.cn-beijing.aliyuncs.com/hohu/hohu-admin"
        )
        w.doc["components"]["backend"]["images"].append(image)
        with self.assertRaises(ValueError):
            self.verify(w)
        self.assertFalse(w.calls)

    def test_pull_failure_is_not_online_success(self):
        w = World()
        with (
            patch.object(w, "pull", side_effect=ValueError("pull failed")),
            self.assertRaises(ValueError),
        ):
            self.verify(w)

    def test_gh_transport_is_fixed_and_errors_redacted(self):
        api = sources.Sources()
        with patch("subprocess.run") as run:
            run.return_value.stdout = b"{}"
            api.request("aihohu/hohu-admin", "releases/tags/v0.1.6")
            args = run.call_args.args[0]
            self.assertIn("--hostname", args)
            self.assertIn("github.com", args)
            self.assertNotIn("--clobber", args)
        with (
            patch("subprocess.run", side_effect=OSError("secret token")),
            self.assertRaisesRegex(ValueError, "GitHub request failed"),
        ):
            api.request("aihohu/hohu-admin", "releases/tags/v0.1.6")


if __name__ == "__main__":
    unittest.main()
