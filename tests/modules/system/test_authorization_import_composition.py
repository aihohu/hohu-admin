"""Import may join an outer AI plan without reopening its transaction."""

import pytest
from sqlalchemy import select

from app.core.authorization_plan import LockMode, TenantLock, TransactionBudget
from app.core.exceptions import BusinessException
from app.modules.system.models.user import User
from app.modules.system.models.user_transfer import UserImportBatch
from app.modules.system.service.authorization_import_candidate import (
    import_authorization_candidate as candidate,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from tests.modules.system.test_authorization_import_candidate import (
    ARTIFACT,
    FILE,
    Storage,
    preview,
    record,
)
from tests.modules.system.test_authorization_import_candidate import (
    import_sessions as import_sessions,
)


async def prepare(db, tenant, records):
    return await candidate.prepare_input(
        db,
        tenant=tenant,
        actor_user_version=1,
        records=records,
        file_bytes=FILE,
        preview_token="preview-1",
        filename="users.xlsx",
        reason="staff import",
        file_storage=Storage(),
        artifact_execution=ARTIFACT,
    )


async def test_import_composes_under_one_outer_proof(import_sessions):
    factory, plain, tenant = import_sessions
    records = [record()]
    await preview(plain, records)
    async with factory() as db:
        command = await prepare(db, tenant, records)
        async with authorization_transactions.scope(
            db,
            tenants=(TenantLock(0, LockMode.EXCLUSIVE),),
            budget=TransactionBudget(timeout_ms=60000),
        ) as tx:
            work = await candidate.plan(db, tx, command)
            await tx.lock(work.locks)
            result = await candidate.apply(db, tx, work)
            assert result.success_count == 1
        await db.commit()
    async with plain() as db:
        assert await db.scalar(select(User.user_id).where(User.user_name == "newuser"))
        assert await db.scalar(select(UserImportBatch.status)) == "SUCCESS"


async def test_import_preparation_cannot_be_replayed(import_sessions):
    factory, plain, tenant = import_sessions
    records = [record()]
    await preview(plain, records)
    async with factory() as db:
        command = await prepare(db, tenant, records)
        with pytest.raises(BusinessException):
            async with authorization_transactions.scope(
                db,
                tenants=(TenantLock(0, LockMode.EXCLUSIVE),),
                budget=TransactionBudget(timeout_ms=60000),
            ) as tx:
                work = await candidate.plan(db, tx, command)
                await tx.lock(work.locks)
                await candidate.apply(db, tx, work)
                await candidate.apply(db, tx, work)
        await db.rollback()
    async with plain() as db:
        assert (
            await db.scalar(select(User.user_id).where(User.user_name == "newuser"))
            is None
        )
