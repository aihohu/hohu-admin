"""Transfer owners publish only current-authorized, manifest-owned artifacts."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from app.core.exceptions import BusinessException
from app.core.file_storage import LocalFileStorage
from app.db import authorization_transfer_owner as owner_module
from app.db.authorization_transfer_owner import TransferOwner
from app.modules.system.models.user_transfer import UserExportTask, UserImportBatch
from app.modules.system.service import user_export_service
from tests.modules.system.test_authorization_import_candidate import (
    import_sessions as import_sessions,
)
from tests.modules.system.test_authorization_system_candidate import (
    TENANT,
    epoch,
)
from tests.modules.system.test_authorization_system_candidate import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)


async def test_preview_owner_uses_exact_bytes_and_manifest(import_sessions, tmp_path):

    factory, plain, tenant = import_sessions
    raw = b"user_name,dept_input,status\npreviewnew,Team,1\n"
    owner = TransferOwner(factory, LocalFileStorage(tmp_path))
    result = await owner.preview(
        tenant=tenant,
        actor_user_version=1,
        file_bytes=raw,
        filename="input.csv",
        reason="audit",
    )
    data, _ = await owner.download(
        tenant=tenant,
        actor_user_version=1,
        kind="preview_source",
        identifier=result.resource_id,
    )
    assert data == raw and result.preview_token
    async with plain() as db:
        batch = await db.get(UserImportBatch, result.resource_id)
        assert batch.status.value == "PREVIEW_DONE" and batch.summary_new == 1


async def test_export_owner_and_download(system_sessions, tmp_path):

    factory, plain = system_sessions
    owner = TransferOwner(factory, LocalFileStorage(tmp_path))
    result = await owner.export(
        tenant=TENANT, actor_user_version=1, args={"reason": "audit"}
    )
    data, filename = await owner.download(
        tenant=TENANT,
        actor_user_version=1,
        kind="export",
        identifier=result.resource_id,
    )
    assert data[:2] == b"PK" and filename.endswith(".xlsx")
    async with plain() as db:
        task = await db.scalar(select(UserExportTask))
        assert (
            task.operator_id == TENANT.actor_user_id and task.status.value == "SUCCESS"
        )
        assert await epoch(db) == 1
        assert len(task.filter_snapshot["data_scope_hash"]) == 64


async def test_cleanup_unknown_commit_ack_never_retires(
    system_sessions, tmp_path, monkeypatch
):
    factory, plain = system_sessions
    storage = LocalFileStorage(tmp_path)
    owner = TransferOwner(factory, storage)
    result = await owner.export(
        tenant=TENANT, actor_user_version=1, args={"reason": "audit"}
    )
    async with plain() as db:
        await db.execute(
            update(UserExportTask).values(
                created_at=(datetime.now(UTC) - timedelta(days=31)).replace(tzinfo=None)
            )
        )
        await db.commit()
    original = owner_module.commit_authorization_session

    async def unknown(db):
        await original(db)
        raise RuntimeError("commit acknowledgement lost")

    monkeypatch.setattr(owner_module, "commit_authorization_session", unknown)
    page = await owner.cleanup_page(kind="export", limit=1)
    assert page.items[0][2] == "retained" and await storage.exists(result.storage_key)


async def test_preview_expiry_can_retire_after_ten_minutes(import_sessions, tmp_path):
    factory, plain, tenant = import_sessions
    storage = LocalFileStorage(tmp_path)
    owner = TransferOwner(factory, storage)
    result = await owner.preview(
        tenant=tenant,
        actor_user_version=1,
        file_bytes=b"user_name,dept_input,status\nexpirepreview,Team,1\n",
        filename="input.csv",
        reason="audit",
    )
    async with plain() as db:
        await db.execute(
            update(UserImportBatch).values(
                created_at=datetime.now(UTC) - timedelta(minutes=11)
            )
        )
        await db.commit()
    page = await owner.cleanup_page(kind="import", limit=1)
    assert page.items[0][2] == "retired" and not await storage.exists(
        result.storage_key
    )


async def test_export_oversize_never_publishes(system_sessions, tmp_path, monkeypatch):

    factory, plain = system_sessions
    monkeypatch.setattr(
        user_export_service, "_build_excel", lambda *_: b"x" * (10 * 1024 * 1024 + 1)
    )
    with pytest.raises(BusinessException):
        await TransferOwner(factory, LocalFileStorage(tmp_path)).export(
            tenant=TENANT, actor_user_version=1, args={"reason": "audit"}
        )
    assert not list(tmp_path.rglob("payload"))
    async with plain() as db:
        assert await db.scalar(select(UserExportTask.export_id)) is None


async def test_cleanup_commits_expiry_before_retirement(system_sessions, tmp_path):
    factory, plain = system_sessions
    storage = LocalFileStorage(tmp_path)
    owner = TransferOwner(factory, storage)
    result = await owner.export(
        tenant=TENANT, actor_user_version=1, args={"reason": "audit"}
    )
    async with plain() as db:
        await db.execute(
            update(UserExportTask).values(
                created_at=(datetime.now(UTC) - timedelta(days=31)).replace(tzinfo=None)
            )
        )
        await db.commit()
    assert (
        await owner._cleanup_phase(
            tenant_id=0, kind="export", identifier=result.resource_id, retire=False
        )
        == "expired"
    )
    page = await owner.cleanup_page(kind="export", limit=1)
    assert page.items == ((0, result.resource_id, "retired"),)
    async with plain() as db:
        assert (
            await db.get(UserExportTask, result.resource_id)
        ).status.value == "EXPIRED"
        assert await epoch(db) == 1
    assert not await storage.exists(result.storage_key)
