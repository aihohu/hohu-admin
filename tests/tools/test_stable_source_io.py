"""Unknown source responses and exhausted read budgets never authorize writes."""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.error import URLError

from tools.ops import stable_release as stable


class StableSourceIOTests(unittest.TestCase):
    def test_reader_bounds_bytes_calls_time_and_redirects(self):
        reader = stable.Reader("unit-secret")
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{"ok": true}'
        reader.opener = Mock()
        reader.opener.open.return_value = response
        self.assertEqual(
            reader.request("aihohu/hohu-admin", "releases/tags/v0.1.6"), {"ok": True}
        )
        request = reader.opener.open.call_args.args[0]
        self.assertEqual(request.method, "GET")
        self.assertTrue(request.full_url.startswith("https://api.github.com/repos/"))
        response.read.assert_called_once_with(stable.LIMIT + 1)
        for raw in (b"[1]", b"not json", b"x" * (stable.LIMIT + 1)):
            response.read.return_value = raw
            with self.subTest(raw_length=len(raw)), self.assertRaises(ValueError):
                reader.request("aihohu/hohu-admin", "releases/tags/v0.1.6")
        for repo, path in (
            ("someone/fork", "branches/main"),
            ("aihohu/hohu-admin", "../secrets"),
            ("aihohu/hohu-admin", "https://evil.invalid"),
        ):
            reader.opener.reset_mock()
            with self.assertRaises(ValueError):
                reader.request(repo, path)
            reader.opener.open.assert_not_called()
        for calls, deadline in ((24, float("inf")), (0, 0)):
            reader.calls, reader.deadline = calls, deadline
            with self.assertRaises(ValueError):
                reader.request("aihohu/hohu-admin", "branches/main")
            reader.opener.open.assert_not_called()
        with self.assertRaises(ValueError):
            stable.NoRedirect().redirect_request(
                None, None, 302, "", {}, "https://evil.invalid"
            )

    def test_network_errors_redact_transport_details(self):
        reader = stable.Reader("unit-secret")
        reader.opener = Mock()
        reader.opener.open.side_effect = URLError("private detail unit-secret")
        with self.assertRaisesRegex(ValueError, "state is unknown") as failure:
            reader.request("aihohu/hohu-admin", "branches/main")
        self.assertNotIn("unit-secret", str(failure.exception))

    def test_default_cli_has_no_alternate_source_or_network(self):
        environment = {
            "GITHUB_REPOSITORY": "aihohu/hohu-admin",
            "GITHUB_SHA": "a" * 40,
            "DEFAULT_BRANCH": "main",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "RELEASE_CHANNEL": "default",
        }
        with tempfile.TemporaryDirectory(prefix="source-cli-") as directory:
            output = Path(directory) / "outputs"
            with (
                patch.dict(os.environ, environment, clear=True),
                patch.object(sys, "argv", ["stable", "--github-output", str(output)]),
                patch.object(stable, "Reader") as reader,
            ):
                stable.main()
                self.assertEqual(
                    output.read_text("utf-8"),
                    "source_sha=" + "a" * 40 + "\nprovenance=\n",
                )
                reader.assert_not_called()
                for override in (
                    {"RELEASE_SOURCE_SHA": "b" * 40},
                    {"RELEASE_PEER_SHA": "b" * 40},
                    {"RELEASE_CHANNEL": "stable-0.1", "RELEASE_MODE": "test"},
                ):
                    output.unlink(missing_ok=True)
                    with (
                        patch.dict(os.environ, override),
                        contextlib.redirect_stderr(io.StringIO()),
                        self.assertRaises(SystemExit),
                    ):
                        stable.main()
                    self.assertFalse(output.exists())
                reader.assert_not_called()

    def test_metadata_rejects_non_text_content_without_attribute_errors(self):
        for content in (None, [], 123, {"malformed": True}):
            request = Mock(
                return_value={
                    "type": "file",
                    "encoding": "base64",
                    "size": 0,
                    "content": content,
                }
            )
            with (
                self.subTest(content=json.dumps(content)),
                self.assertRaises(ValueError),
            ):
                stable.content(request, "aihohu/hohu-admin", "a" * 40, "pyproject.toml")
