"""Product manifest failures are release gates, not advisory warnings."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.ops import check_product_release as check

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures/product_release"


def sample():
    return json.loads((FIXTURES / "valid.json").read_bytes())


def encode(value):
    return json.dumps(value).encode()


class ManifestTests(unittest.TestCase):
    def test_report_url_must_be_the_actual_canonical_resource(self):
        doc = sample()
        doc["evidence"][0]["url"] += "/"
        with self.assertRaises(ValueError):
            check.validate(encode(doc))

    def test_digest_newline_and_overflow_number_are_rejected(self):
        doc = sample()
        doc["evidence"][0]["sha256"] += "\n"
        with self.assertRaises(ValueError):
            check.validate(encode(doc))

    def test_json_numeric_overflow_is_rejected(self):
        with self.assertRaises(ValueError):
            check.decode(b'{"number":1e999}')

    def test_valid_offline_does_not_access_network(self):
        with patch("subprocess.run", side_effect=AssertionError("offline network")):
            self.assertEqual(check.validate(encode(sample())), sample())

    def test_invalid_files(self):
        for path in FIXTURES.glob("invalid-*.json"):
            with self.subTest(path=path.name), self.assertRaises(ValueError):
                check.validate(path.read_bytes())

    def test_strict_json_and_types(self):
        for raw in (b"\xef\xbb\xbf{}", b"\xff", b"{}\n{}", b'{"x":NaN}', b"[]"):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                check.validate(raw)
        for field, value in (
            ("schema_version", True),
            ("schema_version", 1.0),
            ("schema_version", "1"),
            ("status", "draft"),
            ("published_at", "2026-02-30T00:00:00Z"),
            ("published_at", "2026-10-23T08:00:00+00:00"),
        ):
            doc = sample()
            doc[field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                check.validate(encode(doc))

    def test_nested_unknown_missing_and_blank_values(self):
        paths = [
            ("upgrade",),
            ("cli",),
            ("clients", "app"),
            ("docs",),
            ("components", "backend"),
            ("components", "backend", "images", 0),
            ("components", "backend", "images", 0, "platforms", 0),
            ("evidence", 0),
        ]
        for path in paths:
            for operation in ("unknown", "missing"):
                doc = sample()
                node = doc
                for key in path:
                    node = node[key]
                if operation == "unknown":
                    node["extra"] = "value"
                else:
                    node.pop(next(iter(node)))
                with (
                    self.subTest(path=path, operation=operation),
                    self.assertRaises(ValueError),
                ):
                    check.validate(encode(doc))
        for value in ("", "   ", "\n"):
            doc = sample()
            doc["upgrade"]["database_revision"] = value
            with self.assertRaises(ValueError):
                check.validate(encode(doc))

    def test_version_source_client_and_docs_constraints(self):
        changes = [
            (("components", "web", "version"), "0.1.5"),
            (("components", "backend", "tag"), "v0.1.5"),
            (("components", "backend", "repository"), "someone/hohu-admin"),
            (("components", "backend", "commit"), "A" * 40),
            (("upgrade", "from_versions"), ["0.1.4", "0.1.5"]),
            (("cli", "minimum_version"), "0.1.15"),
            (("cli", "tested_versions"), ["0.1.17"]),
            (("clients", "app", "status"), "verified"),
            (("clients", "desktop", "status"), "unsupported"),
            (("docs", "series"), "0.2"),
            (
                ("docs", "snapshot_url"),
                "https://github.com/aihohu/hohu-admin-docs/blob/main/readme.md",
            ),
        ]
        for path, value in changes:
            doc = sample()
            node = doc
            for key in path[:-1]:
                node = node[key]
            node[path[-1]] = value
            with self.subTest(path=path), self.assertRaises(ValueError):
                check.validate(encode(doc))

    def test_evidence_completeness_and_bindings(self):
        for index in range(6):
            doc = sample()
            doc["evidence"].pop(index)
            with self.subTest(index=index), self.assertRaises(ValueError):
                check.validate(encode(doc))
        for index, field, value in [
            (0, "subject", "pair"),
            (0, "backend_digest", "sha256:" + "a" * 64),
            (0, "platform", "linux/amd64"),
            (0, "web_commit", "d" * 40),
            (2, "subject", "backend"),
            (2, "platform", "linux/arm64"),
            (2, "backend_digest", "sha256:" + "b" * 64),
            (2, "web_digest", None),
            (2, "result", "failed"),
        ]:
            doc = sample()
            doc["evidence"][index][field] = value
            with self.subTest(index=index, field=field), self.assertRaises(ValueError):
                check.validate(encode(doc))
        doc = sample()
        doc["evidence"][0].update(
            backend_digest="sha256:" + "a" * 64, platform="linux/amd64"
        )
        check.validate(encode(doc))

    def test_platforms_and_mirrors(self):
        for mutation in (
            "no_primary",
            "two_primaries",
            "duplicate_platform",
            "no_verified",
            "unequal_verified",
            "mirror_digest",
            "mirror_platform",
            "mirror_qualification",
            "duplicate_image",
        ):
            doc = sample()
            images = doc["components"]["backend"]["images"]
            primary = images[0]
            mirror = copy.deepcopy(primary)
            mirror.update(
                role="mirror",
                repository="registry.cn-beijing.aliyuncs.com/hohu/hohu-admin",
            )
            images.append(mirror)
            if mutation == "no_primary":
                primary["role"] = "mirror"
            elif mutation == "two_primaries":
                mirror["role"] = "primary"
            elif mutation == "duplicate_platform":
                primary["platforms"].append(copy.deepcopy(primary["platforms"][0]))
            elif mutation == "no_verified":
                for c in doc["components"].values():
                    for image in c["images"]:
                        for p in image["platforms"]:
                            p["qualification"] = "unverified"
            elif mutation == "unequal_verified":
                doc["components"]["web"]["images"][0]["platforms"][1][
                    "qualification"
                ] = "verified"
            elif mutation == "mirror_digest":
                mirror["index_digest"] = "sha256:" + "d" * 64
            elif mutation == "mirror_platform":
                mirror["platforms"][0]["manifest_digest"] = "sha256:" + "d" * 64
            elif mutation == "mirror_qualification":
                mirror["platforms"][0]["qualification"] = "unverified"
            else:
                images.append(copy.deepcopy(mirror))
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                check.validate(encode(doc))

    def test_no_unsafe_urls_or_image_references(self):
        for url in (
            "http://github.com/a",
            "https://user:secret@github.com/a",
            "https://github.com.evil/a",
            "https://127.0.0.1/a",
            "https://github.com/aihohu/hohu-admin/blob/main/report.json",
            "https://github.com/aihohu/hohu-admin/blob/" + "a" * 40 + "/../secret",
            "https://github.com/aihohu/hohu-admin/releases/download/v0.1.6/x?token=secret",
        ):
            doc = sample()
            doc["evidence"][0]["url"] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                check.validate(encode(doc))

    def test_cli_offline_status_and_draft_upload_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "release.draft.json"
            path.write_bytes(encode(sample()))
            self.assertEqual(check.check_file(path)["online"], "not_run")


if __name__ == "__main__":
    unittest.main()
