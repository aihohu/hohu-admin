"""Loopback HTTP, real JWT/owner and closed PostgreSQL admission together."""

import asyncio
import socket
import traceback
from uuid import uuid4

import httpx
import pytest
from redis.asyncio import Redis
from sqlalchemy import text

from app.core.config import settings
from app.core.security import create_access_token
from tests.scripts.test_authorization_maintenance_database import (
    deployment_database as deployment_database,
)
from tests.scripts.test_controlled_install_database import assert_closed, options
from tools.ops import controlled_install as install
from tools.ops import policy_prepare


@pytest.fixture(autouse=True)
async def isolated_auth_redis(monkeypatch):
    # Real Redis connection, owned by this test's event loop.
    client = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    monkeypatch.setattr("app.modules.auth.service.redis_client", client)
    yield
    await client.aclose()


@pytest.mark.parametrize("bootstrap", [True, False])
async def test_live_preparation_keeps_normal_admission_closed(
    deployment_database, tmp_path, monkeypatch, bootstrap
):
    admin, writer, role, _ = deployment_database
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    args = options(tmp_path, role, monkeypatch, admin, bootstrap=True)
    # A direct upgrade starts from a real seeded installation, with its stop inventory.
    if not bootstrap:
        async with install.ops.PostgresStop(
            admin,
            tmp_path / "initial",
            install.ops.StopManifest(
                "initial", "a" * 40, "direct", ("api", "scheduler")
            ),
            (role,),
        ) as guard:
            await install.prepare(guard, None, True)
            await asyncio.sleep(31)
            await guard.resume_bootstrap()
        args = options(tmp_path, role, monkeypatch, admin, bootstrap=False)
    args.command, args.policy_port = "policy-prepare", port
    original = policy_prepare.serve
    failures = []
    original_execute = policy_prepare.PolicyPreparation._execute_locked

    async def observed_execute(self, *arguments):
        try:
            return await original_execute(self, *arguments)
        except Exception as error:
            failures.append(
                (
                    type(error).__name__,
                    [
                        (frame.name, frame.lineno)
                        for frame in traceback.extract_tb(error.__traceback__)
                    ],
                )
            )
            raise

    monkeypatch.setattr(
        policy_prepare.PolicyPreparation, "_execute_locked", observed_execute
    )

    async def exercise(guard, processes, *, bootstrap, port):
        async with guard.runtime_engine.connect() as db:
            actor = (
                await db.execute(
                    text(
                        "SELECT user_id, auth_version FROM sys_user WHERE tenant_id=0 AND user_name='admin'"
                    )
                )
            ).one()
            rid = await db.scalar(
                text(
                    "SELECT role_id FROM sys_role WHERE tenant_id=0 AND role_code='R_SUPER'"
                )
            )
            version = await db.scalar(
                text("SELECT row_version FROM sys_tenant WHERE tenant_id=0")
            )
        bearer = create_access_token(
            subject=str(actor.user_id),
            tenant_id=0,
            tenant_version=version,
            user_version=actor.auth_version,
        )

        async def requests():
            async with httpx.AsyncClient(
                base_url=f"http://127.0.0.1:{port}",
                timeout=15,
                headers={"Authorization": "Bearer " + bearer},
            ) as client:
                for _ in range(150):
                    try:
                        response = await client.get("/policy-prepare/catalog")
                        break
                    except httpx.ConnectError:
                        await asyncio.sleep(0.1)
                else:
                    pytest.fail("preparation listener did not start")
                assert response.status_code == 200, (response.text, failures)
                assert response.json()["data"]["schemaVersion"] == 2
                assert response.json()["data"]["head"]["activeManifestId"] is None
                assert (await client.get("/system/user/list")).status_code == 404
                assert (
                    await client.get(
                        "/policy-prepare/catalog",
                        headers={"Authorization": "Bearer invalid"},
                    )
                ).status_code == 401
                await assert_closed(admin, writer, tmp_path)
                result = await client.post(
                    "/policy-prepare/command",
                    json={
                        "action": "publish",
                        "requestId": str(uuid4()),
                        "expectedVersion": 1,
                        "reason": "explicit test candidate",
                        "roleId": str(rid),
                        "objectId": "system.user",
                        "bindingVersion": 0,
                        "rules": [
                            {
                                "fieldId": "nickname",
                                "access": {
                                    "operation": "read",
                                    "purpose": "management.read",
                                    "use": "",
                                },
                                "effect": "allow",
                                "level": "plain",
                                "maskId": None,
                            }
                        ],
                    },
                )
                assert result.status_code == 200, result.text
                displayed = (await client.get("/policy-prepare/catalog")).json()[
                    "data"
                ]["bindings"][0]
                assert displayed["rules"][0]["fieldId"] == "nickname"
                result = await client.post(
                    "/policy-prepare/command",
                    json={
                        "action": "publish",
                        "requestId": str(uuid4()),
                        "expectedVersion": 2,
                        "reason": "round-trip displayed rules",
                        "roleId": displayed["roleId"],
                        "objectId": displayed["objectId"],
                        "bindingVersion": displayed["bindingVersion"],
                        "rules": displayed["rules"],
                    },
                )
                assert result.status_code == 200, result.text
                result = await client.post(
                    "/policy-prepare/command",
                    json={
                        "action": "build",
                        "requestId": str(uuid4()),
                        "expectedVersion": 3,
                        "reason": "seal test candidate",
                    },
                )
                assert result.status_code == 200, result.text
                value = result.json()["data"]
                assert (
                    value["candidateManifestDigest"]
                    and value["activeManifestDigest"] is None
                )
                result = await client.post(
                    "/policy-prepare/command",
                    json={
                        "action": "ready",
                        "requestId": str(uuid4()),
                        "expectedVersion": 4,
                        "expectedEpoch": value["epoch"],
                        "reason": "must stay closed",
                    },
                )
                assert result.json()["errorCode"] == "FIELD_POLICY_ACTIVATION_BLOCKED"
                for bad in ("wrong-password", "does-not-exist"):
                    rejected = await client.post(
                        "/policy-prepare/login",
                        json={
                            "tenantCode": "default",
                            "userName": "admin" if bad == "wrong-password" else bad,
                            "password": "wrong-password",
                        },
                    )
                    assert rejected.status_code == 401
                    assert rejected.json()["errorCode"] == "INVALID_CREDENTIALS"
                assert (
                    await client.get("/system/field-policy/catalog")
                ).status_code == 401
                login = await client.post(
                    "/policy-prepare/login",
                    json={
                        "tenantCode": "default",
                        "userName": "admin",
                        "password": "Install-Test-123!",
                    },
                )
                assert login.status_code == 200, login.text
                local = {"Authorization": "Bearer " + login.json()["data"]["token"]}
                assert "." not in login.json()["data"]["token"]
                policy_path = f"/system/field-policy/roles/{rid}/objects/system.user"
                document = (await client.get(policy_path, headers=local)).json()["data"]
                assert document["headVersion"] == "4"
                draft = {
                    "expectedVersion": document["headVersion"],
                    "expectedBindingVersion": document["bindingVersion"],
                    "catalogDigest": document["catalogDigest"],
                    "rules": document["rules"],
                }
                preview = await client.post(
                    policy_path + "/preview", headers=local, json=draft
                )
                assert preview.status_code == 200, preview.text
                request_id = str(uuid4())
                saved = await client.post(
                    policy_path + "/revisions",
                    headers=local,
                    json=draft
                    | {
                        "previewDigest": preview.json()["data"]["previewDigest"],
                        "requestId": request_id,
                        "reason": "real password session",
                    },
                )
                assert saved.status_code == 200, saved.text
                assert saved.json()["data"]["headVersion"] == "5"
                receipt = await client.get(
                    "/system/field-policy/requests/" + request_id, headers=local
                )
                assert receipt.json()["data"] == saved.json()["data"]
                assert saved.json()["data"]["activeManifestDigest"] is None
                await assert_closed(admin, writer, tmp_path)
                assert (
                    await client.post("/policy-prepare/logout", headers=local)
                ).status_code == 200
                assert (await client.get(policy_path, headers=local)).status_code == 401
                events = (guard.directory / "policy-events.jsonl").read_text()
                assert "INVALID_CREDENTIALS" in events
                assert "accepted" in events
                assert login.json()["data"]["token"] not in events
                assert "Install-Test-123!" not in events
                assert "wrong-password" not in events
                assert "real password session" not in events
                assert (await client.post("/policy-prepare/finish")).status_code == 200

        async with asyncio.timeout(60):
            server = asyncio.create_task(
                original(guard, processes, bootstrap=bootstrap, port=port)
            )
            client = asyncio.create_task(requests())
            try:
                await client
                result = await server
            finally:
                for task in (client, server):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(client, server, return_exceptions=True)
        assert result["state"] == "preparing-stopped"
        assert result["business_background_tasks"] == []
        assert install._receipt(guard, bootstrap)["policy_prepare_sha256"]
        return result

    monkeypatch.setattr(policy_prepare, "serve", exercise)
    assert (await install.run(args))["state"] == "prepared-admission-closed"
    await assert_closed(admin, writer, tmp_path)
    assert (tmp_path / "policy-prepare.json").exists()
