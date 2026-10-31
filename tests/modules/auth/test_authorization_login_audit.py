"""Login audit retains nullable attribution without an ordinary write session."""

from uuid import uuid4

import pytest
from sqlalchemy import delete, event, select

from app.db.session import engine
from app.modules.auth.service import auth_service
from app.modules.system.models.login_log import SysLoginLog
from tests.fixture_session import FixtureSessionLocal as AsyncSessionLocal


@pytest.fixture(autouse=True)
async def close_owned_test_pool():
    yield
    await engine.dispose()


@pytest.mark.parametrize("tenant_id", [None, 0])
async def test_login_audit_uses_plan_and_preserves_nullable_tenant(tenant_id):
    name = "audit_" + uuid4().hex[:16]
    writes = []

    def capture(_conn, _cursor, sql, _params, context, _many):
        if sql.startswith("INSERT INTO sys_login_log"):
            writes.append(
                context.execution_options.get("_authorization_plan_permit") is not None
            )

    event.listen(engine.sync_engine, "before_cursor_execute", capture)
    try:
        await auth_service._write_login_log(
            tenant_id=tenant_id,
            audit_scope="unresolved" if tenant_id is None else "tenant",
            user_id=None,
            username=name,
            ip="127.0.0.1",
            user_agent="fixed-test",
            status="2",
            message="凭据无效",
        )
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture)
    try:
        assert writes == [True], writes
        async with AsyncSessionLocal() as db:
            log = await db.scalar(
                select(SysLoginLog).where(SysLoginLog.username == name)
            )
            assert (
                log is not None and log.tenant_id == tenant_id and log.user_id is None
            )
            assert log.status == "2" and log.login_time is not None
    finally:
        async with AsyncSessionLocal() as db, db.begin():
            await db.execute(delete(SysLoginLog).where(SysLoginLog.username == name))
