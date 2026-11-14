"""Real current manifests govern copies, including never-enforced diagnostics."""

from sqlalchemy import insert, select

from app.core.authorization_plan import TenantLock
from app.db.authorization_runtime import AuthorizationRuntime
from app.modules.ai.service.field_authority_runtime import ai_field_authority
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.models.user import User
from app.modules.system.schemas.operation_log import OperationLogQuery
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_relation_field_writes import activate_relations


async def seed_log(plain):
    async with plain() as db:
        await db.execute(
            insert(SysOperationLog).values(
                operation_log_id=901,
                tenant_id=0,
                audit_scope="tenant",
                user_id=101,
                username="OLD-COPIED-SECRET",
                module="system",
                action="update",
                method="PUT",
                path="/user/12345?email=PRIVATE@example.org",
                request_params='{"email":"PRIVATE@example.org"}',
                status_code=200,
                ip="192.168.4.123",
                duration=0,
            )
        )
        await db.commit()


async def read(plain, query=None):

    return await AuthorizationRuntime(plain.kw["bind"]).delivery.logs(
        tenant=TENANT,
        actor_user_version=1,
        kind="operation",
        query=query or OperationLogQuery(),
    )


async def test_log_copy_uses_current_identity_and_never_returns_unknown_raw_text(
    system_sessions,
):
    _, plain = system_sessions
    await seed_log(plain)
    page = await read(plain)
    row = page["records"][0]
    async with plain() as db:
        name = await db.scalar(
            select(User.user_name).where(User.tenant_id == 0, User.user_id == 101)
        )
        raw = await db.get(SysOperationLog, 901)
        assert raw.username == "OLD-COPIED-SECRET" and "PRIVATE" in raw.request_params
    assert row["username"] == name
    assert "request_params" not in row and "requestParams" not in row
    assert "12345" not in row.get("path", "") and "PRIVATE" not in str(row)
    assert row["ip"] != "192.168.4.123" and row["duration"] == 0


async def test_log_field_and_current_user_source_are_independently_required(
    system_sessions, monkeypatch
):
    _, plain = system_sessions
    await seed_log(plain)
    await activate_relations(
        system_sessions,
        monkeypatch,
        missing=(
            ("system.user", "user_name", "read"),
            ("system.operation_log", "duration", "read"),
        ),
    )
    page = await read(plain)
    row = page["records"][0]
    assert "username" not in row and "duration" not in row
    assert row["operationLogId"] == "901"


async def test_generic_ai_watermark_binds_all_objects_and_refuses_old_unknown(
    system_sessions, monkeypatch
):

    factory, plain = system_sessions
    await activate_relations(system_sessions, monkeypatch)
    async with factory() as db:
        async with authorization_transactions.scope(db, tenants=(TenantLock(0),)) as tx:
            value = await ai_field_authority.current(db, tx, tenant=TENANT)
            assert value["schema"] == 2 and value["active_digest"]
            assert not await ai_field_authority.visible(db, tenant=TENANT, frozen=None)
            assert await ai_field_authority.visible(db, tenant=TENANT, frozen=value)
        await db.rollback()
