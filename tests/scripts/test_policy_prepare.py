"""Preparation has a closed local surface and never restores normal admission."""

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from app.core.exceptions import BusinessException
from tools.ops.policy_prepare import (
    PolicyPreparation,
    create_app,
    require_normal_resume,
)


async def test_queued_request_cannot_reenter_finished_preparation():
    controller = object.__new__(PolicyPreparation)
    controller.lock = asyncio.Lock()
    controller.finished = asyncio.Event()
    await controller.lock.acquire()
    pending = asyncio.create_task(controller.execute("token", {"action": "read"}))
    await asyncio.sleep(0)
    controller.finished.set()
    controller.lock.release()
    with pytest.raises(BusinessException) as exc:
        await pending
    assert exc.value.error_code == "FIELD_POLICY_UNAVAILABLE"


async def test_preparation_rejects_duplicate_keys_and_deep_json():
    class Controller:
        async def execute(self, *_):
            pytest.fail("invalid JSON reached the owner")

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(Controller())),
        base_url="http://localhost",
        headers={"Authorization": "Bearer test-token"},
    ) as client:
        for body in (
            b'{"action":"receipt","action":"receipt","request_id":"x"}',
            b"[" * 2000 + b"]" * 2000,
        ):
            assert (
                await client.post("/policy-prepare/command", content=body)
            ).status_code == 400


async def test_only_preparation_routes_exist():
    calls = []

    class Controller:
        async def execute(self, token, command):
            calls.append((token, command))
            return {"headVersion": 1}

        async def finish(self, token):
            calls.append((token, "finish"))
            return {"state": "preparing-admission-closed"}

    app = create_app(Controller())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        for path in ("/system/user", "/auth/register", "/ai/chat", "/docs"):
            assert (await client.get(path)).status_code == 404
        assert (await client.get("/policy-prepare/catalog")).status_code == 401
        response = await client.get(
            "/policy-prepare/catalog", headers={"Authorization": "Bearer test-token"}
        )
        assert response.json() == {
            "code": 200,
            "msg": "操作成功",
            "data": {"headVersion": 1},
        }
        assert calls == [("test-token", {"action": "read"})]


async def test_preparation_rejects_oversized_and_unknown_commands():
    class Controller:
        async def execute(self, *_):
            pytest.fail("invalid input reached the owner")

    app = create_app(Controller())
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers={"Authorization": "Bearer test-token"},
    ) as client:
        assert (
            await client.post("/policy-prepare/command", content=b"x" * (524288 + 1))
        ).status_code == 413
        assert (
            await client.post("/policy-prepare/command", json={"action": "raw-sql"})
        ).status_code == 400


def test_generic_preparation_marker_blocks_every_normal_resume(tmp_path):
    guard = SimpleNamespace(directory=tmp_path)
    require_normal_resume(guard)
    (tmp_path / "policy-prepare.json").write_text(
        '{"state":"preparing"}', encoding="utf-8"
    )
    with pytest.raises(BusinessException) as exc:
        require_normal_resume(guard)
    assert exc.value.error_code == "FIELD_POLICY_ACTIVATION_BLOCKED"
