"""Keep stable application provenance independent of the default controller."""

import base64
import copy
import importlib
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.tools import test_release_build as fixtures
from tools.ops import release_build as build

ROOT = Path(__file__).resolve().parents[2]
REPO = "aihohu/hohu-admin-web" if ROOT.name.endswith("-web") else "aihohu/hohu-admin"
BASELINES = {
    "aihohu/hohu-admin": "71966784b4eb35d0fde9d8c62657137fdc562879",
    "aihohu/hohu-admin-web": "72d2a46d84704517ac7257e3a695fe9f66253f1c",
}
PEER = next(repo for repo in BASELINES if repo != REPO)


class StableSourceFixture:
    def __init__(self):
        self.calls = []
        self.sources = {REPO: "a" * 40, PEER: "b" * 40}
        self.version = "0.1.7"
        self.protected = True
        self.moved = False
        self.ancestor = True
        self.anchor = True
        self.peer_version = None

    def request(self, repo, path):
        self.calls.append((repo, path))
        source = self.sources[repo]
        if path.startswith("branches/"):
            return {
                "name": "release/" + self.version,
                "protected": self.protected,
                "commit": {"sha": "e" * 40 if self.moved else source},
            }
        if path == "releases/tags/v0.1.6":
            return {"tag_name": "v0.1.6", "draft": False, "prerelease": False}
        if path == "git/ref/tags/v0.1.6":
            return {"object": {"type": "tag", "sha": "d" * 40}}
        if path == "git/tags/" + "d" * 40:
            return {
                "tag": "v0.1.6",
                "object": {
                    "type": "commit",
                    "sha": BASELINES[repo] if self.anchor else "e" * 40,
                },
            }
        if path.startswith("compare/"):
            return {
                "status": "ahead" if self.ancestor else "diverged",
                "merge_base_commit": {"sha": BASELINES[repo]},
            }
        if path.startswith("contents/"):
            version = (
                self.peer_version
                if repo == PEER and self.peer_version
                else self.version
            )
            name = path.split("?", 1)[0].removeprefix("contents/")
            values = {
                "pyproject.toml": f'[project]\nname="hohu-admin"\nversion="{version}"\n',
                "uv.lock": f'[[package]]\nname="hohu-admin"\nversion="{version}"\nsource={{editable="."}}\n',
                "package.json": json.dumps({"version": version}),
                "CHANGELOG.md": f"# Changes\n\n## [v{version}]\n\nReviewed repair.\n",
            }
            raw = values[name].encode()
            return {
                "type": "file",
                "encoding": "base64",
                "size": len(raw),
                "content": base64.b64encode(raw).decode(),
            }
        raise AssertionError((repo, path))


class StableReleaseSelectionTests(unittest.TestCase):
    def setUp(self):
        self.stable = importlib.import_module("tools.ops.stable_release")
        self.api = StableSourceFixture()
        self.arguments = {
            "repository": REPO,
            "tag": "v0.1.7",
            "source_sha": "a" * 40,
            "peer_sha": "b" * 40,
            "controller_sha": "c" * 40,
            "default_branch": "main",
            "selected_ref": "refs/heads/main",
            "event": "workflow_dispatch",
        }

    def select(self, **overrides):
        return self.stable.select_stable(
            self.api.request, **(self.arguments | overrides)
        )

    def test_stable_source_can_differ_from_new_series_controller(self):
        selected = self.select()
        self.assertEqual(selected["controller_sha"], "c" * 40)
        self.assertEqual(selected["baseline_sha"], BASELINES[REPO])
        self.assertEqual(selected["source_ref"], "refs/heads/release/0.1.7")
        self.assertEqual(selected["peer_repository"], PEER)
        self.assertEqual(selected["peer_sha"], "b" * 40)
        self.assertEqual({repo for repo, _ in self.api.calls}, set(BASELINES))

    def test_invalid_context_or_versions_rejected_before_network(self):
        for key, value in (
            ("repository", "someone/fork"),
            ("event", "pull_request"),
            ("selected_ref", "refs/heads/release/0.1.7"),
            ("default_branch", ""),
            ("controller_sha", "main"),
            ("source_sha", "a" * 40 + "\n"),
            ("peer_sha", "$(whoami)"),
            ("tag", "v0.2.0"),
            ("tag", "v0.1.6"),
            ("tag", "v0.1.07"),
        ):
            self.api.calls.clear()
            with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                self.select(**{key: value})
            self.assertEqual(self.api.calls, [])

    def test_unprotected_moved_unrelated_or_mismatched_sources_fail_closed(self):
        for field, value in (
            ("protected", False),
            ("moved", True),
            ("ancestor", False),
            ("anchor", False),
            ("peer_version", "0.1.8"),
        ):
            self.api = StableSourceFixture()
            setattr(self.api, field, value)
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.select()

    def test_unknown_remote_state_is_not_permission(self):
        def unavailable(*_):
            raise ValueError("Remote state unknown")

        with self.assertRaises(ValueError):
            self.stable.select_stable(unavailable, **self.arguments)

    def test_readonly_baseline_exception_cannot_select_changed_source(self):
        self.api.version = "0.1.6"
        self.api.sources = dict(BASELINES)
        selected = self.select(
            tag="v0.1.6",
            source_sha=BASELINES[REPO],
            peer_sha=BASELINES[PEER],
            verify_only=True,
        )
        self.assertEqual(selected["baseline_sha"], BASELINES[REPO])
        with self.assertRaises(ValueError):
            self.select(tag="v0.1.6", verify_only=True)


class StableBuildEvidenceTests(unittest.TestCase):
    def make_build(self):
        fixture = fixtures.BuildEvidenceTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        expected = build.identity(
            REPO, "a" * 40, "v0.1.7", "123", None, provenance=self.provenance()
        )
        preflight = build.preflight(
            expected, exclusive_writer="true", probe=lambda *_: None
        )
        record = build.freeze_build(
            expected, fixture.digest, preflight, inspect=fixture.inspect
        )
        raw = build.canonical(record)
        api = fixtures.GitHubFixture(expected, raw)
        api.run.update(head_sha="c" * 40, head_branch="main")
        api.job["head_sha"] = "c" * 40
        api.artifact["workflow_run"]["head_sha"] = "c" * 40
        return fixture, expected, record, raw, api

    def provenance(self):
        return {
            "channel": "stable-0.1",
            "controller_sha": "c" * 40,
            "source_ref": "refs/heads/release/0.1.7",
            "baseline_tag": "v0.1.6",
            "baseline_sha": BASELINES[REPO],
            "peer_repository": PEER,
            "peer_sha": "b" * 40,
        }

    def test_identity_freezes_both_controller_and_application_without_altering_v1(self):
        old = build.identity(REPO, "a" * 40, "v0.1.7", "123", None)
        provenance = self.provenance()
        new = build.identity(
            REPO, "a" * 40, "v0.1.7", "123", None, provenance=provenance
        )
        self.assertEqual(old["schema_version"], 1)
        self.assertNotIn("provenance", old)
        self.assertEqual(new["schema_version"], 2)
        self.assertEqual(new["source_sha"], "a" * 40)
        self.assertEqual(new["provenance"]["controller_sha"], "c" * 40)
        provenance["peer_sha"] = "e" * 40
        self.assertEqual(new["provenance"]["peer_sha"], "b" * 40)

    def test_invalid_provenance_cannot_downgrade_to_v1(self):
        for name, value in (
            ("channel", "default"),
            ("controller_sha", "main"),
            ("source_ref", "refs/heads/main"),
            ("baseline_sha", "e" * 40),
            ("peer_repository", REPO),
            ("peer_sha", "HEAD"),
            ("extra", True),
        ):
            provenance = copy.deepcopy(self.provenance())
            provenance[name] = value
            with self.subTest(name=name), self.assertRaises(ValueError):
                build.identity(
                    REPO, "a" * 40, "v0.1.7", "123", None, provenance=provenance
                )

    def test_original_controller_run_restores_pinned_application_record(self):
        fixture, expected, record, raw, api = self.make_build()
        self.assertEqual(build.restore(api, expected), (raw, record))
        build.verify_source(record, inspect=fixture.inspect)

    def test_application_sha_cannot_impersonate_controller_run(self):
        _, expected, _, _, api = self.make_build()
        api.run["head_sha"] = api.job["head_sha"] = "a" * 40
        api.artifact["workflow_run"]["head_sha"] = "a" * 40
        with self.assertRaises(ValueError):
            build.restore(api, expected)

    def test_v2_recovery_retains_all_original_build_failure_gates(self):
        for mutation in ("branch", "attempt", "job", "artifact", "record"):
            _, expected, _, raw, api = self.make_build()
            if mutation == "branch":
                api.run["head_branch"] = "release/0.1.7"
            elif mutation == "attempt":
                api.run["run_attempt"] = 2
            elif mutation == "job":
                api.job["conclusion"] = "failure"
            elif mutation == "artifact":
                api.artifact["workflow_run"]["head_sha"] = "e" * 40
            else:
                changed = json.loads(raw)
                changed["provenance"]["peer_sha"] = "e" * 40
                api.reset_archive(build.canonical(changed))
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                build.restore(api, expected)

    def test_environment_binds_provenance_to_actual_controller(self):
        expected = build.identity(
            REPO, "a" * 40, "v0.1.7", "123", None, provenance=self.provenance()
        )
        env = {
            "GITHUB_REPOSITORY": REPO,
            "GITHUB_SHA": "c" * 40,
            "GITHUB_RUN_ID": "123",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main",
            "DEFAULT_BRANCH": "main",
            "RELEASE_TAG": "v0.1.7",
            "RELEASE_EXCLUSIVE_WRITER_CONFIRMED": "true",
            "RELEASE_SOURCE_SHA": "a" * 40,
            "RELEASE_PROVENANCE": json.dumps(self.provenance()),
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(build.environment_identity(), expected)
        for key, value in (
            ("GITHUB_SHA", "e" * 40),
            ("RELEASE_PROVENANCE", ""),
            ("RELEASE_SOURCE_SHA", "main"),
        ):
            with (
                self.subTest(key=key),
                patch.dict(os.environ, env | {key: value}, clear=True),
                self.assertRaises(ValueError),
            ):
                build.environment_identity()


if __name__ == "__main__":
    unittest.main()
