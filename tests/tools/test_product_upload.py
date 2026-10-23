"""Immutable upload recovery, including a real HTTP response disconnect."""

import json
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import Request, urlopen

from tools.ops import upload_product_release as upload
from tools.ops.product_release_sources import Sources

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures/product_release/valid.json"


class MemorySource:
    def __init__(self):
        self.raw = None
        self.posts = 0
        self.mode = "normal"
        self.verified = 0

    def verify(self, _doc, _work_dir):
        self.verified += 1
        if self.mode == "verify_failure":
            raise ValueError("unqualified")

    def release(self, _repo, tag):
        return {"id": 1, "tag_name": tag, "draft": False, "prerelease": False}

    def assets(self, _repo, _release):
        if self.mode == "query_unknown" or (
            self.mode == "readback_unknown" and self.posts
        ):
            raise ValueError("503 unknown")
        item = {
            "id": 1,
            "name": "release.json",
            "state": "uploaded",
            "size": len(self.raw or b""),
        }
        if self.mode == "truncated":
            return [item] * 100
        if self.mode == "duplicate":
            return [item, item]
        return [item] if self.raw is not None else []

    def download_asset(self, _repo, _asset):
        return self.raw

    def upload(self, _repo, _tag, path):
        self.posts += 1
        if self.mode == "no_write_ack":
            raise ValueError("lost ack")
        self.raw = b"different bytes" if self.mode == "race" else path.read_bytes()
        if self.mode in {"lost_ack", "race", "readback_unknown"}:
            raise ValueError("lost ack")


class UploadTests(unittest.TestCase):
    def test_create_retry_and_ack_loss(self):
        for mode in ("normal", "lost_ack"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                path = root / "release.json"
                path.write_bytes(FIXTURE.read_bytes())
                api = MemorySource()
                api.mode = mode
                upload.upload_manifest(path, api, root)
                upload.upload_manifest(path, api, root)
                self.assertEqual(api.posts, 1)
                self.assertEqual(api.raw, path.read_bytes())
                self.assertEqual(api.verified, 2)

    def test_failures_never_blindly_retry_or_overwrite(self):
        for mode in (
            "different",
            "query_unknown",
            "duplicate",
            "truncated",
            "race",
            "no_write_ack",
            "readback_unknown",
            "verify_failure",
        ):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as d:
                root = Path(d)
                path = root / "release.json"
                path.write_bytes(FIXTURE.read_bytes())
                api = MemorySource()
                api.mode = mode
                if mode == "different":
                    api.raw = b"other"
                with self.assertRaises(ValueError):
                    upload.upload_manifest(path, api, root)
                self.assertEqual(
                    api.posts,
                    1 if mode in {"race", "no_write_ack", "readback_unknown"} else 0,
                )

    def test_draft_filename_and_changed_bytes_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path = root / "release.draft.json"
            path.write_bytes(FIXTURE.read_bytes())
            api = MemorySource()
            with self.assertRaises(ValueError):
                upload.upload_manifest(path, api, root)
            final = root / "release.json"
            final.write_bytes(FIXTURE.read_bytes())
            with (
                patch.object(
                    api, "verify", side_effect=lambda *_: final.write_bytes(b"changed")
                ),
                self.assertRaises(ValueError),
            ):
                upload.upload_manifest(final, api, root)
            self.assertEqual(api.posts, 0)

    def test_production_upload_command_has_no_overwrite_or_delete(self):
        with patch("subprocess.run") as run:
            Sources().upload("aihohu/hohu-admin", "v0.1.6", Path("release.json"))
            args = run.call_args.args[0]
            self.assertEqual(args[:3], ["gh", "release", "upload"])
            self.assertNotIn("--clobber", args)
            self.assertIn("github.com/aihohu/hohu-admin", args)

    def test_real_http_upload_persists_then_loses_response(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stored = root / "server-asset.bin"
            writes = []

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *_args):
                    pass

                def do_GET(self):
                    if self.path == "/assets":
                        result = (
                            [
                                {
                                    "id": 1,
                                    "name": "release.json",
                                    "state": "uploaded",
                                    "size": stored.stat().st_size,
                                }
                            ]
                            if stored.exists()
                            else []
                        )
                        payload = json.dumps(result).encode()
                    else:
                        payload = stored.read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

                def do_POST(self):
                    writes.append(self.path)
                    stored.write_bytes(
                        self.rfile.read(int(self.headers["Content-Length"]))
                    )
                    # Persistence is complete, but the HTTP acknowledgement is lost.
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_port}"

            class HTTPSource(Sources):
                def verify(self, _doc, _work_dir):
                    # This integration test isolates upload/asset transport.
                    # Source qualification has its own full mutation matrix.
                    pass

                def release(self, _repo, tag):
                    return {
                        "id": 1,
                        "tag_name": tag,
                        "draft": False,
                        "prerelease": False,
                    }

                def api_bytes(
                    self, _repo, path, *, accept="application/vnd.github+json"
                ):
                    endpoint = (
                        "/asset" if accept == "application/octet-stream" else "/assets"
                    )
                    self_path = (
                        "releases/assets/1"
                        if endpoint == "/asset"
                        else "releases/1/assets?per_page=100"
                    )
                    if path != self_path:
                        raise ValueError("Unexpected integration endpoint")
                    with urlopen(base + endpoint, timeout=5) as response:
                        return response.read()

                def upload(self, _repo, _tag, path):
                    try:
                        with urlopen(
                            Request(
                                base + "/upload", data=path.read_bytes(), method="POST"
                            ),
                            timeout=5,
                        ) as response:
                            response.read()
                    except (URLError, OSError) as error:
                        raise ValueError("HTTP acknowledgement lost") from error

            try:
                path = root / "release.json"
                path.write_bytes(FIXTURE.read_bytes())
                source = HTTPSource()
                upload.upload_manifest(path, source, root)
                upload.upload_manifest(path, source, root)
                self.assertEqual(stored.read_bytes(), path.read_bytes())
                self.assertEqual(writes, ["/upload"])
                changed = json.loads(path.read_bytes())
                changed["limitations"].append("Conflicting upload")
                path.write_text(json.dumps(changed), encoding="utf8")
                with self.assertRaises(ValueError):
                    upload.upload_manifest(path, source, root)
                self.assertEqual(writes, ["/upload"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
