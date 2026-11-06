"""The actual production factory cannot issue a legacy mutation or SQL escape."""

import pytest
from sqlalchemy import event, func, insert, literal, select, text

from app.core.exceptions import BusinessException
from app.db.authorization_plan_session import AuthorizationPlanSession
from app.db.session import AsyncSessionLocal
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User
from app.utils.pagination import QueryParams, paginate, paginate_custom
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


@pytest.mark.parametrize(
    "operation",
    ["dml", "flush", "lock", "raw", "driver", "function", "run_sync", "stream"],
)
async def test_actual_default_factory_rejects_unplanned_sql_before_execution(
    plan_sessions, operation
):
    _, plain = plan_sessions
    executed = []
    engine = plain.kw["bind"]

    def capture(_conn, _cursor, sql, *_args):
        executed.append(sql)

    async with AsyncSessionLocal(bind=engine) as db:
        event.listen(engine.sync_engine, "after_cursor_execute", capture)
        try:
            with pytest.raises(BusinessException) as error:
                if operation == "dml":
                    await db.execute(
                        insert(User).values(
                            tenant_id=0,
                            user_id=901,
                            user_name="never",
                            hashed_password="unused",
                        )
                    )
                elif operation == "flush":
                    db.add(
                        User(
                            tenant_id=0,
                            user_id=902,
                            user_name="never",
                            hashed_password="unused",
                        )
                    )
                    await db.flush()
                elif operation == "lock":
                    await db.execute(select(User).with_for_update())
                elif operation == "raw":
                    await db.execute(text("SELECT 1"))
                elif operation == "driver":
                    await (await db.connection()).exec_driver_sql("SELECT 1")
                elif operation == "function":
                    await db.scalar(select(func.pg_sleep(0)))
                elif operation == "run_sync":
                    await db.run_sync(lambda session: session.execute(text("SELECT 1")))
                else:
                    await db.stream(select(User))
            assert error.value.error_code == "FIELD_POLICY_LOCK_ORDER"
            assert not executed
        finally:
            event.remove(engine.sync_engine, "after_cursor_execute", capture)
            await db.rollback()


async def test_actual_default_factory_reads_and_cannot_disable_plan_requirement(
    plan_sessions,
):
    _, plain = plan_sessions
    async with AsyncSessionLocal(bind=plain.kw["bind"]) as db:
        assert isinstance(db, AuthorizationPlanSession)
        assert db._authorization_journal.require_plan is True
        assert await db.scalar(select(func.pg_catalog.count(User.user_id))) == 2
        assert await db.scalar(select(literal(1))) == 1
    with pytest.raises(BusinessException):
        AsyncSessionLocal(bind=plain.kw["bind"], require_plan=False)


@pytest.mark.parametrize("custom", [False, True])
async def test_default_factory_retains_pagination_totals(plan_sessions, custom):
    _, plain = plan_sessions
    async with AsyncSessionLocal(bind=plain.kw["bind"]) as db:
        if custom:
            result = await paginate_custom(db, select(User.user_id), size=1)
        else:
            result = await paginate(db, Tenant, QueryParams(size=1))
        assert result.total == 2 and len(result.records) == 1
