"""REST FILE ingress retains its response and uses only planned writers."""

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event, insert, select, update
from sqlalchemy.ext.asyncio import async_sessionmaker
from starlette.datastructures import UploadFile

from app.core.exceptions import setup_exception_handlers
from app.core.file_storage import LocalFileStorage
from app.core.security import create_access_token
from app.db import authorization_http, authorization_runtime, session
from app.db.request_session import RequestSession
from app.modules.auth import service
from app.modules.system.api import user
from app.modules.system.models.setting import SystemSetting
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from app.modules.system.models.user_transfer import UserExportTask, UserImportBatch
from app.modules.system.service import user_export_service
from tests.modules.system.test_authorization_import_candidate import (
    import_sessions as import_sessions,
)

RAW = b"user_name,dept_input,status\nhttpimport,Team,1\n"


@pytest.fixture
async def transfer_http(import_sessions, monkeypatch, tmp_path):
    _, plain, _ = import_sessions
    engine = plain.kw["bind"]
    monkeypatch.setattr(authorization_http, "engine", engine)
    monkeypatch.setattr(
        authorization_runtime, "get_file_storage", lambda: LocalFileStorage(tmp_path)
    )
    monkeypatch.setattr(
        user_export_service, "get_file_storage", lambda: LocalFileStorage(tmp_path)
    )
    monkeypatch.setattr(
        session,
        "AsyncSessionLocal",
        async_sessionmaker(engine, class_=RequestSession, expire_on_commit=False),
    )
    monkeypatch.setattr(service, "_is_blacklisted", AsyncMock(return_value=False))
    app = FastAPI()
    setup_exception_handlers(app)
    app.include_router(user.router, prefix="/user")
    token = create_access_token(
        subject="101", tenant_id=0, tenant_version=1, user_version=1
    )
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": f"Bearer {token}"},
    ) as client:
        yield client, plain


async def send_import(client, *, raw=RAW, **form):
    return await client.post(
        "/user/import",
        files={"file": ("input.csv", raw, "text/csv")},
        data={"reason": "HTTP FILE audit", **form},
    )


async def test_preview_formal_replay_have_one_commit_and_no_transaction_upload(
    transfer_http, monkeypatch
):
    client, plain = transfer_http
    engine = plain.kw["bind"].sync_engine
    active, commits, locks = set(), [], []

    def begin(connection):
        active.add(connection)

    def rollback(connection):
        active.discard(connection)

    def commit(connection):
        active.discard(connection)
        commits.append(connection)

    def sql(_connection, _cursor, statement, _params, _context, _many):
        if "FOR UPDATE" in statement or "FOR SHARE" in statement:
            locks.append(statement)

    original = UploadFile.read

    async def read(file, size=-1):
        assert not active, "UploadFile IO must follow request rollback"
        assert 0 < size <= 10 * 1024 * 1024 + 1
        return await original(file, size)

    monkeypatch.setattr(UploadFile, "read", read)
    listeners = [
        ("begin", begin),
        ("rollback", rollback),
        ("commit", commit),
        ("before_cursor_execute", sql),
    ]
    for name, listener in listeners:
        event.listen(engine, name, listener)
    try:
        preview = await send_import(client, dry_run="true")
        assert preview.status_code == 200, preview.text
        data = preview.json()["data"]
        assert (
            data["newCount"] == 1 and data["newRecords"][0]["userName"] == "httpimport"
        )
        assert data["expiresAt"] and len(commits) == 1
        assert "sys_user_field_policy_global_guard" in locks[0]
        for _ in range(2):
            commits.clear()
            locks.clear()
            formal = await send_import(client, preview_token=data["previewToken"])
            assert formal.status_code == 200, formal.text
            assert len(commits) == 1
            assert "sys_user_field_policy_global_guard" in locks[0]
    finally:
        for name, listener in listeners:
            event.remove(engine, name, listener)
    async with plain() as db:
        users = (
            await db.scalars(select(User).where(User.user_name == "httpimport"))
        ).all()
        assert len(users) == 1
        assert await db.scalar(select(UserFieldPolicyState.epoch)) == 3


async def test_preview_rechecks_actor_after_upload(transfer_http, monkeypatch):
    client, plain = transfer_http
    original = UploadFile.read

    async def read(file, size=-1):
        data = await original(file, size)
        async with plain() as db:
            await db.execute(
                update(User).where(User.user_id == 101).values(auth_version=2)
            )
            await db.commit()
        return data

    monkeypatch.setattr(UploadFile, "read", read)
    response = await send_import(client, dry_run="true")
    assert response.status_code == 401, response.text
    async with plain() as db:
        assert await db.scalar(select(UserImportBatch.batch_id)) is None


async def test_preview_rechecks_policy_after_upload(transfer_http, monkeypatch):
    client, plain = transfer_http
    original = UploadFile.read

    async def read(file, size=-1):
        data = await original(file, size)
        async with plain() as db:
            await db.execute(
                insert(SystemSetting).values(
                    tenant_id=0,
                    setting_id=502,
                    setting_key="upload:import_max_bytes",
                    setting_value="1",
                    status="1",
                )
            )
            await db.commit()
        return data

    monkeypatch.setattr(UploadFile, "read", read)
    response = await send_import(client, dry_run="true")
    assert response.status_code == 400, response.text
    assert response.json()["errorCode"] == "AI_IMPORT_FILE_TOO_LARGE"
    async with plain() as db:
        assert await db.scalar(select(UserImportBatch.batch_id)) is None


@pytest.mark.parametrize("dry_run", ["true", "false"])
async def test_import_preserves_field_errors(transfer_http, dry_run):
    client, _ = transfer_http
    response = await send_import(
        client,
        raw=b"user_name,dept_input,status\nx,Team,invalid\n",
        dry_run=dry_run,
        preview_token="invalid-but-parser-first",
    )
    assert response.status_code == 400, response.text
    assert response.json()["errorCode"] == "AI_IMPORT_FIELD_ERRORS"
    assert response.json()["data"]["errors"][0]["rowNum"] == 2


async def test_export_http_uses_one_planned_commit(transfer_http):
    client, plain = transfer_http
    engine = plain.kw["bind"].sync_engine
    locks, commits = [], []

    def sql(_conn, _cursor, statement, _params, _context, _many):
        if "FOR UPDATE" in statement or "FOR SHARE" in statement:
            locks.append(statement)

    def commit(connection):
        commits.append(connection)

    event.listen(engine, "before_cursor_execute", sql)
    event.listen(engine, "commit", commit)
    try:
        response = await client.post("/user/export", json={"reason": "HTTP export"})
    finally:
        event.remove(engine, "before_cursor_execute", sql)
        event.remove(engine, "commit", commit)
    assert response.status_code == 200, response.text
    assert response.content[:2] == b"PK"
    assert len(commits) == 1
    assert locks and "sys_user_field_policy_global_guard" in locks[0]
    assert response.headers["content-disposition"].endswith(".xlsx")


@pytest.mark.parametrize("legacy_cross_owner", [False, True])
async def test_rest_download_planned_and_legacy_super_compatible(
    transfer_http, legacy_cross_owner
):
    client, plain = transfer_http
    if legacy_cross_owner:
        storage = authorization_runtime.get_file_storage()
        data = b"PK old export fixture"
        path = await storage.save(
            data,
            mime_type="application/octet-stream",
            suffix=".xlsx",
            namespace="user-export",
        )
        async with plain() as db:
            await db.execute(
                insert(User).values(
                    tenant_id=0,
                    user_id=102,
                    user_name="oldexporter",
                    hashed_password="fixture",
                    status="1",
                )
            )
            await db.execute(
                insert(UserExportTask).values(
                    tenant_id=0,
                    export_id="legacy-export",
                    operator_id=102,
                    filter_snapshot={},
                    reason="old export",
                    status="SUCCESS",
                    file_storage_key=path,
                    file_size_bytes=len(data),
                    row_count=1,
                )
            )
            await db.commit()
        identifier = "legacy-export"
    else:
        exported = await client.post("/user/export", json={"reason": "download audit"})
        assert exported.status_code == 200, exported.text
        data = exported.content
        async with plain() as db:
            identifier = await db.scalar(select(UserExportTask.export_id))
    locks = []

    def sql(_conn, _cursor, statement, _params, _context, _many):
        if "FOR UPDATE" in statement or "FOR SHARE" in statement:
            locks.append(statement)

    engine = plain.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", sql)
    try:
        response = await client.get(f"/user/export/{identifier}/download")
    finally:
        event.remove(engine, "before_cursor_execute", sql)
    assert response.status_code == 200, response.text
    assert response.content == data
    assert locks and "sys_user_field_policy_global_guard" in locks[0]
    async with plain() as db:
        assert await db.scalar(select(UserFieldPolicyState.epoch)) == 2


async def test_cancel_preview_commits_before_retiring_file(transfer_http):
    client, plain = transfer_http
    preview = await send_import(client, dry_run="true")
    assert preview.status_code == 200, preview.text
    async with plain() as db:
        batch = await db.scalar(select(UserImportBatch))
        identifier, path = batch.batch_id, batch.file_storage_key
    locks, commits = [], []
    engine = plain.kw["bind"].sync_engine

    def sql(_conn, _cursor, statement, _params, _context, _many):
        if "FOR UPDATE" in statement or "FOR SHARE" in statement:
            locks.append(statement)

    def commit(connection):
        commits.append(connection)

    event.listen(engine, "before_cursor_execute", sql)
    event.listen(engine, "commit", commit)
    try:
        response = await client.post(
            f"/user/import/{identifier}/cancel", json={"reason": "cancel preview"}
        )
    finally:
        event.remove(engine, "before_cursor_execute", sql)
        event.remove(engine, "commit", commit)
    assert response.status_code == 200, response.text
    assert response.json()["data"]["status"] == "CANCELLED"
    assert locks and "sys_user_field_policy_global_guard" in locks[0]
    assert len(commits) == 2
    async with plain() as db:
        batch = await db.get(UserImportBatch, identifier)
        assert batch.status.value == "CANCELLED" and batch.file_storage_key is None
        assert await db.scalar(select(UserFieldPolicyState.epoch)) == 2
    assert not await authorization_runtime.get_file_storage().exists(path)


async def test_rest_import_errors_have_batch_bound_manifest(transfer_http):
    client, plain = transfer_http
    raw = b"user_name,dept_input,status\nvaliduser,Team,1\nbaddept,Missing,1\n"
    preview = await send_import(client, raw=raw, dry_run="true")
    assert preview.status_code == 200, preview.text
    response = await send_import(
        client, raw=raw, preview_token=preview.json()["data"]["previewToken"]
    )
    assert response.status_code == 200, response.text
    async with plain() as db:
        batch = await db.scalar(select(UserImportBatch))
        assert batch.failed_count == 1
        artifact = await authorization_runtime.get_file_storage().artifact_identity(
            batch.failed_rows_file
        )
        assert artifact.operation_id == "rest-import:" + batch.batch_id
