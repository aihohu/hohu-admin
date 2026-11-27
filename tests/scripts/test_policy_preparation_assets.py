"""Only the explicitly selected immutable Web bundle is exposed."""

import hashlib
import json

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from tools.ops.policy_preparation_assets import PreparationAssets


async def test_standard_console_entry_serves_same_verified_role_route(tmp_path):
    content = (
        b'<!doctype html><html><head></head><body><div id="app"></div></body></html>'
    )
    (tmp_path / "index.html").write_bytes(content)
    (tmp_path / "policy-preparation.json").write_text(
        json.dumps(
            {
                "protocol": "field-management/2",
                "version": "0.2.0",
                "sourceCommit": "b" * 40,
                "files": {"index.html": hashlib.sha256(content).hexdigest()},
            }
        ),
        encoding="utf8",
    )
    app = FastAPI()
    PreparationAssets(tmp_path, "b" * 40).mount(app)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        for path in ("/", "/login", "/system/role"):
            response = await client.get(path)
            assert response.status_code == 200
            assert (
                b'<meta name="hohu-admission" content="field-management/2">'
                in response.content
            )
        for path in ("/system/user/list", "/api/auth/login", "/policy-prepare.html"):
            assert (await client.get(path)).status_code == 404


def bundle(tmp_path):
    content = b"<html><head></head><body>console</body></html>"
    (tmp_path / "index.html").write_bytes(content)
    manifest = {
        "protocol": "field-management/2",
        "version": "0.2.0",
        "sourceCommit": "b" * 40,
        "files": {"index.html": hashlib.sha256(content).hexdigest()},
    }
    (tmp_path / "policy-preparation.json").write_text(
        json.dumps(manifest), encoding="utf8"
    )
    return manifest


def test_bundle_requires_matching_commit_and_hash(tmp_path):
    bundle(tmp_path)
    assert (
        PreparationAssets(tmp_path, "b" * 40)
        .read("index.html")[0]
        .startswith(b"<html>")
    )
    with pytest.raises(ValueError):
        PreparationAssets(tmp_path, "c" * 40)
    (tmp_path / "index.html").write_text("changed", encoding="utf8")
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
