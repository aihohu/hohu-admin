"""Scheduled retention uses the fixed planned owner and preserves evidence."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select, update

from app.core.file_storage import LocalFileStorage
from app.db import authorization_http, authorization_runtime
from app.db.authorization_transfer_owner import TransferOwner
from app.modules.system.models.user_transfer import UserImportBatch, UserImportBatchLog
from app.tasks import user_cleanup_tasks as tasks
from tests.modules.system.test_authorization_import_candidate import (
    import_sessions as import_sessions,
)


@pytest.fixture
async def retention_case(import_sessions, tmp_path, monkeypatch):
    factory, plain, tenant = import_sessions
    storage = LocalFileStorage(tmp_path)
    monkeypatch.setattr(authorization_http, "engine", plain.kw["bind"])
    monkeypatch.setattr(authorization_runtime, "get_file_storage", lambda: storage)

    def denied():
        raise AssertionError("Scheduled cleanup must not open a legacy writer")

    monkeypatch.setattr(tasks, "AsyncSessionLocal", denied, raising=False)
    monkeypatch.setattr(tasks, "_cursors", {}, raising=False)
    owner = TransferOwner(factory, storage)
    failures = []
    original = TransferOwner._cleanup_phase

    async def phase(self, **request):
        try:
            return await original(self, **request)
        except Exception as exc:
            failures.append(exc)
            raise

    monkeypatch.setattr(TransferOwner, "_cleanup_phase", phase)
    preview = await owner.preview(
        tenant=tenant,
        actor_user_version=1,
        file_bytes=b"user_name,dept_input,status\nretentionuser,Team,1\n",
        filename="input.csv",
        reason="retention test",
    )
    yield plain, owner, storage, preview
    if failures:
        raise failures[0]


@pytest.mark.parametrize(
    "status,age,eligible",
    [
        ("SUCCESS", 31, False),
        ("SUCCESS", 91, True),
        ("CREATED", 91, False),
        ("RUNNING", 91, False),
    ],
)
async def test_scheduled_import_retention_respects_ninety_days_and_terminal_state(
    retention_case, status, age, eligible
):
    plain, _, storage, preview = retention_case
    async with plain() as db:
        await db.execute(
            update(UserImportBatch).values(
                status=status, created_at=datetime.now(UTC) - timedelta(days=age)
            )
        )
        await db.commit()
    count = await tasks.clean_expired_import_batches()
    assert count == int(eligible)
    assert await storage.exists(preview.storage_key) is not eligible
    async with plain() as db:
        row = await db.get(UserImportBatch, preview.resource_id)
        assert row is not None, (
            "Retirement must preserve historical completion metadata"
        )
        assert row.status.value == ("EXPIRED" if eligible else status)


async def test_scheduled_preview_expiry_uses_g_first_and_keeps_expiry_log(
    retention_case,
):
    plain, _, storage, preview = retention_case
    async with plain() as db:
        await db.execute(
            update(UserImportBatch).values(
                created_at=datetime.now(UTC) - timedelta(minutes=11)
            )
        )
        await db.commit()
    locks = []

    def sql(_connection, _cursor, statement, _params, _context, _many):
        if "FOR UPDATE" in statement or "FOR SHARE" in statement:
            locks.append(statement)

    engine = plain.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", sql)
    try:
        count = await tasks.clean_expired_import_previews()
    finally:
        event.remove(engine, "before_cursor_execute", sql)
    assert count == 1 and not await storage.exists(preview.storage_key)
    assert "sys_user_field_policy_global_guard" in locks[0]
    async with plain() as db:
        row = await db.get(UserImportBatch, preview.resource_id)
        assert row.status.value == "EXPIRED" and row.finished_at is not None
        logs = (
            await db.scalars(
                select(UserImportBatchLog).where(UserImportBatchLog.event == "EXPIRED")
            )
        ).all()
        assert len(logs) == 1
        assert logs[0].from_status == "PREVIEW_DONE" and logs[0].to_status == "EXPIRED"
    assert await tasks.clean_expired_import_previews() == 0
