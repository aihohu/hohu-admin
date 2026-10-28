"""Generic uploads and retirement use production HTTP with real JWTs."""

import asyncio
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image
from sqlalchemy import event, select, update

from app.core.authorization_plan import (
    LockMode,
    LockPlan,
    ResourceKey,
    ResourceKind,
    TenantLock,
)
from app.core.config import settings
from app.core.exceptions import BusinessException
from app.db import authorization_system_owner, authorization_upload_owner
from app.db.authorization_runtime import AuthorizationRuntime
from app.modules.system.api import file
from app.modules.system.models.file import File
from app.modules.system.models.user import User
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from tests.db.test_authorization_runtime_concurrency import CommitGate
from tests.modules.system.test_authorization_system_http import (
    http_system as http_system,
)
from tests.modules.system.test_authorization_system_http import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_authorization_system_http import (
    system_sessions as system_sessions,
)


@pytest.fixture
async def file_http(http_system, monkeypatch, tmp_path):
    client, plain = http_system
    client._transport.app.include_router(file.router, prefix="/file")
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path / "public"))
    monkeypatch.setattr(settings, "PRIVATE_UPLOAD_DIR", str(tmp_path / "private"))
    stream = BytesIO()
    Image.new("RGB", (2, 2), "red").save(stream, format="PNG")
    yield client, plain, stream.getvalue()


@pytest.mark.parametrize("batch", [False, True])
async def test_file_writes_g_first_single_ack(file_http, batch):
    client, plain, content = file_http
    locks, commits = [], []
    engine = plain.kw["bind"].sync_engine

    def sql(_c, _cursor, statement, _params, _ctx, _many):
        if "FOR UPDATE" in statement or "FOR SHARE" in statement:
            locks.append(statement)

    def committed(c):
        commits.append(c)

    event.listen(engine, "before_cursor_execute", sql)
    event.listen(engine, "commit", committed)
    try:
        response = await client.post(
            "/file/batch-upload" if batch else "/file/upload",
            files={"files" if batch else "file": ("image.png", content, "image/png")},
        )
        assert response.status_code == 200, response.text
        assert len(commits) == 1
        assert locks and "sys_user_field_policy_global_guard" in locks[0]
        result = response.json()["data"]
        identifier = int((result[0] if batch else result)["fileId"])
        async with plain() as db:
            record = await db.get(File, identifier)
            assert (record.tenant_id, record.owner_user_id) == (0, 101)
            path = Path(record.file_path)
            assert await asyncio.to_thread(path.read_bytes) == content
        locks.clear()
        commits.clear()
        if batch:
            response = await client.post("/file/batch-delete", json=[identifier])
        else:
            response = await client.delete(f"/file/{identifier}")
        assert response.status_code == 200, response.text
        assert len(commits) == 1
        assert locks and "sys_user_field_policy_global_guard" in locks[0]
        assert not await asyncio.to_thread(path.exists)
        async with plain() as db:
            assert (
                await db.scalar(select(File.file_id).where(File.file_id == identifier))
                is None
            )
    finally:
        event.remove(engine, "before_cursor_execute", sql)
        event.remove(engine, "commit", committed)


async def test_upload_io_has_no_live_database_transaction(file_http, monkeypatch):
    client, plain, content = file_http
    active, calls = set(), []
    engine = plain.kw["bind"].sync_engine
    original = authorization_upload_owner.publish_uploads

    def begin(c):
        active.add(c)

    def finish(c):
        active.discard(c)

    def publish(*args):
        assert not active
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(authorization_upload_owner, "publish_uploads", publish)
    for name, callback in (("begin", begin), ("commit", finish), ("rollback", finish)):
        event.listen(engine, name, callback)
    try:
        response = await client.post(
            "/file/upload", files={"file": ("x.png", content, "image/png")}
        )
        assert response.status_code == 200, response.text
        assert calls == [True]
    finally:
        for name, callback in (
            ("begin", begin),
            ("commit", finish),
            ("rollback", finish),
        ):
            event.remove(engine, name, callback)


async def test_upload_revocation_after_disk_write_rejects_metadata(
    file_http, monkeypatch
):
    client, plain, content = file_http
    original = authorization_upload_owner.file_transform_worker.run

    async def revoke(function, *args, **kwargs):
        result = await original(function, *args, **kwargs)
        async with plain() as db:
            await db.execute(
                update(User).where(User.user_id == 101).values(auth_version=2)
            )
            await db.commit()
        return result

    monkeypatch.setattr(authorization_upload_owner.file_transform_worker, "run", revoke)
    response = await client.post(
        "/file/upload", files={"file": ("x.png", content, "image/png")}
    )
    assert response.status_code == 401, response.text
    async with plain() as db:
        assert (await db.scalars(select(File.file_id))).all() == []


@pytest.mark.parametrize("delete", [False, True])
@pytest.mark.parametrize("committed", [False, True])
async def test_file_unknown_ack_preserves_disk(
    file_http, monkeypatch, delete, committed
):
    client, plain, content = file_http
    identifier = None
    if delete:
        response = await client.post(
            "/file/upload", files={"file": ("x.png", content, "image/png")}
        )
        identifier = int(response.json()["data"]["fileId"])
    original = authorization_upload_owner.commit_authorization_session

    async def lost(db):
        if committed:
            await original(db)
        raise BusinessException(
            503, "Unknown ACK", error_code="AUTHORIZATION_COMMIT_UNKNOWN"
        )

    monkeypatch.setattr(
        authorization_upload_owner, "commit_authorization_session", lost
    )
    if delete:
        response = await client.delete(f"/file/{identifier}")
    else:
        response = await client.post(
            "/file/upload", files={"file": ("x.png", content, "image/png")}
        )
    assert response.status_code == 503, response.text
    paths = await asyncio.to_thread(
        lambda: list(Path(settings.UPLOAD_DIR).rglob("*.png"))
    )
    assert len(paths) == 1
    assert await asyncio.to_thread(paths[0].read_bytes) == content
    async with plain() as db:
        rows = (await db.scalars(select(File.file_id))).all()
        assert len(rows) == int(not committed if delete else committed)


async def test_batch_delete_missing_member_is_atomic(file_http):
    client, plain, content = file_http
    response = await client.post(
        "/file/upload", files={"file": ("x.png", content, "image/png")}
    )
    identifier = int(response.json()["data"]["fileId"])
    response = await client.post("/file/batch-delete", json=[identifier, 99999])
    assert response.status_code == 404, response.text
    async with plain() as db:
        row = await db.get(File, identifier)
        assert await asyncio.to_thread(Path(row.file_path).read_bytes) == content


@pytest.mark.parametrize("business_type", ["ai-chat", "user-import"])
async def test_private_upload_keeps_authenticated_scope(file_http, business_type):
    client, plain, _ = file_http
    response = await client.post(
        "/file/upload",
        data={"business_type": business_type, "owner_user_id": "999"},
        files={"file": ("data.csv", b"user_name\nalice\n", "text/csv")},
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["fileUrl"] == ""
    async with plain() as db:
        record = await db.get(File, int(response.json()["data"]["fileId"]))
        assert (record.tenant_id, record.owner_user_id) == (0, 101)
        assert Path(record.file_path).is_relative_to(settings.PRIVATE_UPLOAD_DIR)


@pytest.mark.parametrize("boundary", ["shared", "wrong_kind", "wrong_path"])
async def test_file_retirement_proof_cannot_bypass_lock_or_cas(file_http, boundary):
    client, plain, content = file_http
    response = await client.post(
        "/file/upload", files={"file": ("x.png", content, "image/png")}
    )
    identifier = int(response.json()["data"]["fileId"])
    async with plain() as db:
        path = (await db.get(File, identifier)).file_path
    key = ResourceKey(
        ResourceKind.USER if boundary == "wrong_kind" else ResourceKind.FILE,
        0,
        101 if boundary == "wrong_kind" else identifier,
    )
    async with AuthorizationRuntime(plain.kw["bind"]).sessions() as db:
        with pytest.raises(BusinessException) as caught:
            async with authorization_transactions.scope(
                db,
                tenants=(
                    TenantLock(
                        0,
                        LockMode.SHARED if boundary == "shared" else LockMode.EXCLUSIVE,
                    ),
                ),
            ) as tx:
                await tx.lock(LockPlan(existing=(key,)))
                await tx.delete_file_fact(
                    key,
                    expected={
                        "owner_user_id": 101,
                        "file_path": "wrong" if boundary == "wrong_path" else path,
                        "del_flag": "0",
                    },
                )
        assert caught.value.error_code in {
            "FIELD_POLICY_LOCK_ORDER",
            "AUTHORIZATION_SNAPSHOT_STALE",
        }
    async with plain() as db:
        assert await db.get(File, identifier) is not None


@pytest.mark.parametrize("revoke_first", [False, True])
async def test_upload_and_rest_revoke_observe_database_wait_both_orders(
    file_http, monkeypatch, revoke_first
):
    client, plain, content = file_http
    gate = CommitGate(monkeypatch, plain.kw["bind"])
    monkeypatch.setattr(
        authorization_upload_owner,
        "commit_authorization_session",
        authorization_system_owner.commit_authorization_session,
    )

    async def upload():
        return await client.post(
            "/file/upload", files={"file": ("x.png", content, "image/png")}
        )

    async def revoke():
        return await client.put("/user/101/revoke-sessions")

    first = asyncio.create_task(
        revoke() if revoke_first else upload(), name="first-owner"
    )
    second = None
    try:
        await asyncio.wait_for(gate.entered.wait(), 5)
        second = asyncio.create_task(
            upload() if revoke_first else revoke(), name="second-owner"
        )
        await gate.assert_blocked(plain)
        assert not second.done()
        gate.release.set()
        responses = await asyncio.wait_for(asyncio.gather(first, second), 5)
        assert responses[0].status_code == 200, responses[0].text
        assert responses[1].status_code == (401 if revoke_first else 200), responses[
            1
        ].text
        async with plain() as db:
            rows = (await db.scalars(select(File.file_id))).all()
            assert len(rows) == int(not revoke_first)
            assert (
                await db.scalar(select(User.auth_version).where(User.user_id == 101))
                == 2
            )
    finally:
        gate.close()
        await asyncio.gather(
            first, *([second] if second is not None else []), return_exceptions=True
        )
