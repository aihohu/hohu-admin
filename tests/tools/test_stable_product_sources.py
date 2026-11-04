"""A synthetic paired stable release is not a public publication claim."""

import json
import unittest
from unittest.mock import Mock, patch

from tests.tools import test_product_sources as fixtures
from tools.ops import product_release_sources as sources
from tools.ops import release_build as build
from tools.ops import release_registry as registry
from tools.ops import stable_release as stable


class StableSourceTransportTests(unittest.TestCase):
    def test_transport_accepts_only_the_exact_pinned_compare_path(self):
        api = sources.Sources()
        path = "compare/" + "a" * 40 + "..." + "b" * 40
        with patch.object(
            sources.subprocess, "run", return_value=Mock(stdout=b"{}")
        ) as run:
            self.assertEqual(api.request("aihohu/hohu-admin", path), {})
            self.assertIn("repos/aihohu/hohu-admin/" + path, run.call_args.args[0])
            run.reset_mock()
            for invalid in (
                "compare/main...release/0.1.7",
                "compare/../secrets",
                path + "/../other",
                path + "?page=2",
            ):
                with self.subTest(path=invalid), self.assertRaises(ValueError):
                    api.request("aihohu/hohu-admin", invalid)
            run.assert_not_called()


def replace_version(value):
    if isinstance(value, str):
        return value.replace("0.1.6", "0.1.7")
    if isinstance(value, bytes):
        return value.replace(b"0.1.6", b"0.1.7")
    if isinstance(value, dict):
        return {replace_version(k): replace_version(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return tuple(replace_version(v) for v in value)
    if isinstance(value, list):
        return [replace_version(v) for v in value]
    return value


def stable_world():
    world = fixtures.World()
    for attr in ("doc", "responses", "files", "assets_data", "manifests"):
        setattr(world, attr, replace_version(getattr(world, attr)))
    for key, component in world.doc["components"].items():
        repo, sha = component["repository"], component["commit"]
        peer = world.doc["components"]["web" if key == "backend" else "backend"]
        run_id = component["build_run_url"].rsplit("/", 1)[1]
        provenance = {
            "channel": "stable-0.1",
            "controller_sha": "c" * 40,
            "source_ref": "refs/heads/release/0.1.7",
            "baseline_tag": "v0.1.6",
            "baseline_sha": stable.BASELINES[repo],
            "peer_repository": peer["repository"],
            "peer_sha": peer["commit"],
        }
        expected = build.identity(
            repo, sha, component["tag"], run_id, None, provenance=provenance
        )
        record = json.loads(world.assets_data[repo, "component-build.json"])
        record.update(expected)
        record["preflight"] = build.preflight(
            expected, exclusive_writer="true", probe=lambda *_: None
        )
        raw = build.canonical(record)
        world.assets_data[repo, "component-build.json"] = raw
        delivery = json.loads(world.assets_data[repo, "component-delivery.json"])
        delivery["build_record_sha256"] = registry.digest(raw)
        world.assets_data[repo, "component-delivery.json"] = build.canonical(delivery)
        world.responses[repo, f"actions/runs/{run_id}/attempts/1"]["head_sha"] = (
            "c" * 40
        )
        jobs = world.responses[
            repo, f"actions/runs/{run_id}/attempts/1/jobs?per_page=100"
        ]
        for job in jobs["jobs"]:
            job["head_sha"] = "c" * 40
        names = ["Validate web release request"]
        if key == "backend":
            names += [
                "Verify release source / Ruff",
                "Verify release source / Hosted release qualification (CI profile)",
            ]
            names += [
                f"Verify release source / Pytest (Python {v})"
                for v in ("3.12", "3.13", "3.14")
            ]
        jobs["jobs"] += [dict(jobs["jobs"][0], name=name) for name in names]
        jobs["total_count"] = len(jobs["jobs"])
        world.responses[repo, "releases/tags/v0.1.6"] = {
            "tag_name": "v0.1.6",
            "draft": False,
            "prerelease": False,
        }
        world.responses[repo, "git/ref/tags/v0.1.6"] = {
            "object": {"type": "tag", "sha": "f" * 40}
        }
        world.responses[repo, "git/tags/" + "f" * 40] = {
            "tag": "v0.1.6",
            "object": {"type": "commit", "sha": stable.BASELINES[repo]},
        }
        world.responses[repo, f"compare/{stable.BASELINES[repo]}...{sha}"] = {
            "status": "ahead",
            "merge_base_commit": {"sha": stable.BASELINES[repo]},
        }
    return world


class StableProductSourcesTests(unittest.TestCase):
    def verify(self, world):
        fixtures.SourcesTests().verify(world)

    def test_paired_sources_remain_valid_after_main_has_advanced(self):
        world = stable_world()
        self.verify(world)
        self.assertEqual(len(world.pulls), 2)

    def test_wrong_controller_source_or_failed_original_ci_is_rejected(self):
        for mutation in ("controller", "ancestor", "ci", "peer"):
            world = stable_world()
            component = world.doc["components"]["backend"]
            repo, sha = component["repository"], component["commit"]
            run_id = component["build_run_url"].rsplit("/", 1)[1]
            if mutation == "controller":
                world.responses[repo, f"actions/runs/{run_id}/attempts/1"][
                    "head_sha"
                ] = sha
            elif mutation == "ancestor":
                world.responses[repo, f"compare/{stable.BASELINES[repo]}...{sha}"][
                    "status"
                ] = "diverged"
            elif mutation == "ci":
                jobs = world.responses[
                    repo, f"actions/runs/{run_id}/attempts/1/jobs?per_page=100"
                ]["jobs"]
                next(
                    job for job in jobs if job["name"].endswith("Pytest (Python 3.14)")
                )["conclusion"] = "failure"
            else:
                record = json.loads(world.assets_data[repo, "component-build.json"])
                record["provenance"]["peer_sha"] = "e" * 40
                record["preflight"]["identity"]["provenance"]["peer_sha"] = "e" * 40
                raw = build.canonical(record)
                world.assets_data[repo, "component-build.json"] = raw
                delivery = json.loads(
                    world.assets_data[repo, "component-delivery.json"]
                )
                delivery["build_record_sha256"] = registry.digest(raw)
                world.assets_data[repo, "component-delivery.json"] = build.canonical(
                    delivery
                )
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.verify(world)
