"""Drive the installer like the role drawer; the controller runs in its own PID."""

import asyncio
import json
import os
import socket
import sys
from uuid import uuid4

import httpx

from app.modules.system.field_policy.ordinary_catalog import production_catalog
from tests.core.test_generic_activation_configuration import qualification_document
from tools.ops import controlled_install as install


async def complete_install(args, monkeypatch):
    qualification = args.control_dir / "test-qualification.json"
    qualification.write_text(
        json.dumps(qualification_document(production_catalog(), "a" * 40)),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        install.ops.settings, "FIELD_POLICY_QUALIFICATION_FILE", str(qualification)
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    command = [
        sys.executable,
        "-m",
        "tools.ops.controlled_install",
        "prepare",
        "--inventory",
        str(args.inventory),
        "--control-dir",
        str(args.control_dir),
        "--policy-port",
        str(port),
        "--console-origin",
        f"http://127.0.0.1:{port}",
        "--policy-web-directory",
        str(args.policy_web_directory),
        "--policy-web-commit",
        args.policy_web_commit,
    ]
    if args.bootstrap:
        command.append("--bootstrap")
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=install.ROOT,
        stdout=asyncio.subprocess.PIPE,
        env={
            **os.environ,
            "RELEASE_BUILD_SHA": "a" * 40,
            "FIELD_POLICY_QUALIFICATION_FILE": str(qualification),
        },
    )
    try:
        async with (
            asyncio.timeout(120),
            httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}", trust_env=False, timeout=20
            ) as client,
        ):
            while True:
                assert process.returncode is None, "Installer exited before login"
                try:
                    response = await client.post(
                        "/policy-prepare/login",
                        json={
                            "tenantCode": "default",
                            "userName": "admin",
                            "password": "Install-Test-123!",
                        },
                    )
                    break
                except httpx.ConnectError:
                    await asyncio.sleep(0.1)
            assert response.status_code == 200, response.text
            client.headers["Authorization"] = (
                "Bearer " + response.json()["data"]["token"]
            )
            root = "/system/field-policy"

            async def call(method, path, **kwargs):
                result = await client.request(method, root + path, **kwargs)
                assert result.status_code == 200, result.text
                return result.json()["data"]

            catalog = await call("GET", "/catalog")
            assert catalog["objects"]
            roles = await call("GET", "/roles")
            target = "/roles/" + roles["records"][0]["roleId"] + "/objects/system.user"
            document = await call("GET", target)
            draft = {
                "expectedVersion": document["headVersion"],
                "expectedBindingVersion": document["bindingVersion"],
                "catalogDigest": document["catalogDigest"],
                "rules": document["rules"],
            }
            preview = await call("POST", target + "/preview", json=draft)
            await call(
                "POST",
                target + "/revisions",
                json={
                    **draft,
                    "requestId": str(uuid4()),
                    "reason": "test explicit role save",
                    "previewDigest": preview["previewDigest"],
                },
            )
            manifest = await call("GET", "/manifests/current")
            draft = {
                "expectedVersion": manifest["headVersion"],
                "catalogDigest": manifest["catalogDigest"],
            }
            preview = await call("POST", "/manifests/preview", json=draft)
            await call(
                "POST",
                "/manifests",
                json={
                    **draft,
                    "requestId": str(uuid4()),
                    "reason": "test explicit manifest",
                    "previewDigest": preview["previewDigest"],
                },
            )
            for action in ("ready", "activate"):
                manifest = await call("GET", "/manifests/current")
                await call(
                    "POST",
                    "/manifests/" + manifest["candidateManifestId"] + "/" + action,
                    json={
                        "requestId": str(uuid4()),
                        "reason": action,
                        "expectedVersion": manifest["headVersion"],
                        "expectedEpoch": manifest["epoch"],
                        "catalogDigest": manifest["catalogDigest"],
                        "candidateManifestDigest": manifest["candidateManifestDigest"],
                    },
                )
            result = await client.post("/policy-prepare/finish")
            assert result.status_code == 200, result.text
            assert result.json()["data"]["state"] == "qualified-admission-closed"
            output, _ = await process.communicate()
            assert process.returncode == 0
            return json.loads(output)
    finally:
        if process.returncode is None:
            process.terminate()
            await process.wait()
