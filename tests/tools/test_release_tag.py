"""Test run-owned release tags with an in-memory GitHub API, never a registry."""

import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from tools.ops.release_tag import GitHub, manage_tag

SHA = "a" * 40
TAG_SHA = "b" * 40
REPOSITORY = "aihohu/hohu-admin"
MESSAGE = "Release v0.1.5\n\nGitHub Actions run: https://github.com/aihohu/hohu-admin/actions/runs/123\n"


class FakeGitHub:
    def __init__(self):
        self.ref = None
        self.tag_object = None
        self.writes = []

    def request(self, method, path, payload=None, *, missing_ok=False):
        if method == "GET" and path == "git/ref/tags/v0.1.5":
            if self.ref is None and not missing_ok:
                raise ValueError("Missing tag")
            return self.ref
        if method == "GET" and path == "git/tags/" + TAG_SHA:
            return self.tag_object
        self.writes.append((method, path, payload))
        if method == "POST" and path == "git/tags":
            self.tag_object = {
                "tag": payload["tag"],
                "message": payload["message"],
                "object": {"type": payload["type"], "sha": payload["object"]},
            }
            return {"sha": TAG_SHA}
        if method == "POST" and path == "git/refs":
            self.ref = {"object": {"type": "tag", "sha": payload["sha"]}}
            return self.ref
        raise AssertionError((method, path, payload))


class ReleaseTagTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeGitHub()

    def manage(self, action, **kwargs):
        return manage_tag(
            self.api,
            action,
            tag="v0.1.5",
            sha=kwargs.get("sha", SHA),
            repository=REPOSITORY,
            run_id=kwargs.get("run_id", "123"),
        )

    def test_preflight_does_not_create_tag(self):
        self.manage("check")
        self.assertEqual(self.api.writes, [])

    def test_create_annotated_tag_pins_source_and_run(self):
        self.manage("create")
        self.assertEqual(self.api.tag_object["object"], {"type": "commit", "sha": SHA})
        self.assertEqual(self.api.tag_object["message"], MESSAGE)
        self.assertEqual(
            self.api.writes[1],
            ("POST", "git/refs", {"ref": "refs/tags/v0.1.5", "sha": TAG_SHA}),
        )

    def test_same_run_retry_is_read_only(self):
        self.manage("create")
        self.api.writes.clear()
        for action in ("check", "create", "verify"):
            self.manage(action)
        self.assertEqual(self.api.writes, [])

    def test_new_run_cannot_reuse_published_version_even_with_same_commit(self):
        self.manage("create")
        self.api.writes.clear()
        for action in ("check", "create", "verify"):
            with self.assertRaisesRegex(ValueError, "another run"):
                self.manage(action, run_id="124")
        self.assertEqual(self.api.writes, [])

    def test_moved_or_manually_created_tag_is_rejected_without_writes(self):
        self.manage("create")
        self.api.writes.clear()
        with self.assertRaises(ValueError):
            self.manage("create", sha="c" * 40)
        self.api.tag_object["message"] = "Manual release"
        with self.assertRaises(ValueError):
            self.manage("create")
        self.api.ref["object"]["type"] = "commit"
        with self.assertRaises(ValueError):
            self.manage("create")
        self.assertEqual(self.api.writes, [])

    def test_verify_missing_tag_fails(self):
        with self.assertRaises(ValueError):
            self.manage("verify")
        self.assertEqual(self.api.writes, [])

    def test_ref_creation_failure_does_not_overwrite_or_delete(self):
        original = self.api.request

        def request(method, path, payload=None, **kwargs):
            if method == "POST" and path == "git/refs":
                raise ValueError("Ref creation failed")
            return original(method, path, payload, **kwargs)

        with (
            patch.object(self.api, "request", side_effect=request),
            self.assertRaises(ValueError),
        ):
            self.manage("create")
        self.assertEqual([item[0] for item in self.api.writes], ["POST"])

    def test_api_only_treats_explicit_404_as_absent(self):
        api = GitHub(REPOSITORY, "unused-test-token")
        for code in (401, 403, 409, 422, 500):
            with (
                self.subTest(code=code),
                patch(
                    "tools.ops.release_tag.urlopen",
                    side_effect=HTTPError(
                        "https://api.github.com", code, "error", {}, None
                    ),
                ),
                self.assertRaises(ValueError),
            ):
                api.request("GET", "git/ref/tags/v0.1.5", missing_ok=True)
        with patch(
            "tools.ops.release_tag.urlopen",
            side_effect=HTTPError("https://api.github.com", 404, "error", {}, None),
        ):
            self.assertIsNone(
                api.request("GET", "git/ref/tags/v0.1.5", missing_ok=True)
            )
            with self.assertRaises(ValueError):
                api.request("POST", "git/refs")

    def test_network_errors_do_not_expose_credentials(self):
        api = GitHub(REPOSITORY, "unused-test-token")
        with (
            patch(
                "tools.ops.release_tag.urlopen",
                side_effect=URLError("unused-test-token"),
            ),
            self.assertRaises(ValueError) as error,
        ):
            api.request("GET", "git/ref/tags/v0.1.5", missing_ok=True)
        self.assertNotIn("unused-test-token", str(error.exception))


if __name__ == "__main__":
    unittest.main()
