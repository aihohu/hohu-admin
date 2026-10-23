"""Exercise authenticated OCI responses over a real isolated HTTP connection."""

import base64
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from tools.ops import release_registry as registry


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.state = {
            "status": 404,
            "code": "MANIFEST_UNKNOWN",
            "listing": 200,
            "challenge": False,
            "realm": "https://ghcr.io/token",
            "seen": [],
            "repository": "aihohu/hohu-admin",
        }
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                state["seen"].append((self.path, self.headers.get("Authorization")))
                if self.path == "/v2/":
                    if state["challenge"] or not self.headers.get("Authorization"):
                        self.send_response(401)
                        challenge = (
                            f'Bearer realm="{state["realm"]}",service="ghcr.io"'
                            if state["challenge"]
                            else 'Basic realm="isolated-registry"'
                        )
                        self.send_header("WWW-Authenticate", challenge)
                        self.end_headers()
                        return
                    status, payload = 200, {}
                elif self.path.startswith(("/token", "/auth")):
                    status, payload = 200, {"token": "test-bearer"}
                elif "/tags/list" in self.path:
                    status = state["listing"]
                    if (
                        state["challenge"]
                        and self.headers.get("Authorization") != "Bearer test-bearer"
                    ):
                        self.send_response(401)
                        self.send_header(
                            "WWW-Authenticate",
                            f'Bearer realm="{state["realm"]}",service="ghcr.io"',
                        )
                        self.end_headers()
                        return
                    payload = {"name": state["repository"], "tags": ["0.1.5"]}
                else:
                    status = state["status"]
                    payload = (
                        {"errors": [{"code": state["code"]}]}
                        if status != 200
                        else {"schemaVersion": 2}
                    )
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                if status == 302:
                    self.send_header("Location", "/must-not-follow")
                if state.get("digest"):
                    self.send_header("Docker-Content-Digest", state["digest"])
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def client(self, image="ghcr.io/aihohu/hohu-admin"):
        self.state["repository"] = image.split("/", 1)[1]

        def transport(url, headers):
            parsed = urlsplit(url)
            return registry.http_get(
                f"http://127.0.0.1:{self.server.server_port}{parsed.path}"
                + ("?" + parsed.query if parsed.query else ""),
                headers,
            )

        return registry.Registry(image, "fake-user", "fake-password", get=transport)

    def test_beijing_acr_accepts_only_its_verified_external_token_endpoint(self):
        image = "registry.cn-beijing.aliyuncs.com/hohu/hohu-admin"
        self.state.update(
            challenge=True, realm="https://dockerauth.cn-hangzhou.aliyuncs.com/auth"
        )
        self.assertIsNone(self.client(image).manifest("0.1.6"))
        self.assertEqual(self.state["seen"][-1][1], "Bearer test-bearer")
        for realm in (
            "https://dockerauth.cn-hangzhou.aliyuncs.com/other",
            "https://dockerauth.cn-hangzhou.aliyuncs.com/auth?redirect=1",
            "https://dockerauth.cn-hangzhou.aliyuncs.com.evil.invalid/auth",
            "http://dockerauth.cn-hangzhou.aliyuncs.com/auth",
        ):
            self.state.update(realm=realm, seen=[])
            with self.subTest(realm=realm), self.assertRaises(registry.RegistryError):
                self.client(image).manifest("0.1.6")
            self.assertEqual(len(self.state["seen"]), 1)
        self.state.update(
            realm="https://dockerauth.cn-hangzhou.aliyuncs.com/auth", seen=[]
        )
        with self.assertRaises(registry.RegistryError):
            self.client().manifest("0.1.6")
        self.assertEqual(len(self.state["seen"]), 1)

    def test_authenticated_explicit_absence(self):
        self.assertIsNone(self.client().manifest("0.1.6"))
        self.assertEqual(len(self.state["seen"]), 4)
        expected = "Basic " + base64.b64encode(b"fake-user:fake-password").decode()
        self.assertIsNone(self.state["seen"][0][1])
        self.assertTrue(all(auth == expected for _, auth in self.state["seen"][1:]))

    def test_bearer_challenge_authenticates_before_accepting_absence(self):
        self.state["challenge"] = True
        self.assertIsNone(self.client().manifest("0.1.6"))
        self.assertEqual(self.state["seen"][-1][1], "Bearer test-bearer")
        self.assertIn("pull%2Cpush", self.state["seen"][1][0])

    def test_bad_credentials_and_unknown_repository_are_not_absence(self):
        for status in (401, 403, 404, 429, 500):
            with self.subTest(status=status):
                self.state["listing"] = status
                self.state["seen"].clear()
                with self.assertRaises(registry.RegistryError):
                    self.client().manifest("0.1.6")
                self.assertFalse(any("/manifests/" in p for p, _ in self.state["seen"]))

    def test_ambiguous_manifest_response_never_becomes_absence(self):
        for status, code in (
            (401, "UNAUTHORIZED"),
            (403, "DENIED"),
            (404, "NAME_UNKNOWN"),
            (404, "DENIED"),
            (429, "TOOMANYREQUESTS"),
            (500, "UNKNOWN"),
            (302, "UNKNOWN"),
        ):
            with self.subTest(status=status, code=code):
                self.state.update(status=status, code=code)
                with self.assertRaises(registry.RegistryError):
                    self.client().manifest("0.1.6")
        self.assertFalse(any("must-not-follow" in p for p, _ in self.state["seen"]))

    def test_untrusted_token_realm_never_receives_credentials(self):
        for realm in (
            "https://evil.invalid/token",
            "http://ghcr.io/token",
            "https://user:pass@ghcr.io/token",
            "https://ghcr.io/token?q=x",
        ):
            with self.subTest(realm=realm):
                self.state.update(challenge=True, realm=realm)
                self.state["seen"].clear()
                with self.assertRaises(registry.RegistryError):
                    self.client().manifest("0.1.6")
                self.assertEqual(len(self.state["seen"]), 1)

    def test_existing_manifest_digest_is_checked(self):
        self.state["status"] = 200
        self.assertEqual(
            registry.decode(self.client().manifest("0.1.6")), {"schemaVersion": 2}
        )
        self.state["digest"] = "sha256:" + "a" * 64
        with self.assertRaisesRegex(registry.RegistryError, "digest"):
            self.client().manifest("0.1.6")

    def test_credentials_and_json_are_strict(self):
        with self.assertRaises(registry.RegistryError):
            registry.Registry("ghcr.io/aihohu/hohu-admin", "", "")
        with self.assertRaises(registry.RegistryError):
            registry.decode('{"errors":[],"errors":[{"code":"MANIFEST_UNKNOWN"}]}')

    def test_anonymous_success_does_not_prove_credentials_were_accepted(self):
        def ignores_credentials(url, _headers):
            if "/manifests/" in url:
                return 404, {}, b'{"errors":[{"code":"MANIFEST_UNKNOWN"}]}'
            return 200, {}, b'{"name":"aihohu/hohu-admin","tags":[]}'

        client = registry.Registry(
            "ghcr.io/aihohu/hohu-admin", "wrong", "wrong", get=ignores_credentials
        )
        with self.assertRaises(registry.RegistryError):
            client.manifest("0.1.6")


if __name__ == "__main__":
    unittest.main()
