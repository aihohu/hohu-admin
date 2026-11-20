"""Only the explicitly selected immutable Web bundle is exposed."""

import hashlib
import json

import pytest

from tools.ops.policy_preparation_assets import PreparationAssets


def bundle(tmp_path):
    content = b"<html>controlled preparation</html>"
    (tmp_path / "policy-prepare.html").write_bytes(content)
    manifest = {
        "protocol": "field-management/1",
        "version": "0.2.0",
        "sourceCommit": "b" * 40,
        "files": {"policy-prepare.html": hashlib.sha256(content).hexdigest()},
    }
    (tmp_path / "policy-preparation.json").write_text(
        json.dumps(manifest), encoding="utf8"
    )
    return manifest


def test_bundle_requires_matching_commit_and_hash(tmp_path):
    bundle(tmp_path)
    assert (
        PreparationAssets(tmp_path, "b" * 40)
        .read("policy-prepare.html")[0]
        .startswith(b"<html>")
    )
    with pytest.raises(ValueError):
        PreparationAssets(tmp_path, "c" * 40)
    (tmp_path / "policy-prepare.html").write_text("changed", encoding="utf8")
    with pytest.raises(ValueError):
        PreparationAssets(tmp_path, "b" * 40)


def test_path_traversal_and_unregistered_assets_are_rejected(tmp_path):
    manifest = bundle(tmp_path)
    assets = PreparationAssets(tmp_path, "b" * 40)
    for path in ("../.env", "policy-preparation.json", "absent.js"):
        with pytest.raises(KeyError):
            assets.read(path)
    manifest["files"]["../.env"] = "f" * 64
    (tmp_path / "policy-preparation.json").write_text(
        json.dumps(manifest), encoding="utf8"
    )
    with pytest.raises(ValueError):
        PreparationAssets(tmp_path, "b" * 40)
