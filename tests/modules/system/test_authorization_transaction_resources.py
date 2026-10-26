"""Execute complete cross-domain plans against the actual registered tables."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, insert, select

from app.core.authorization_plan import (
    LockMode,
    LockPlan,
    NewResource,
    ResourceKey,
    ResourceKind,
    TenantLock,
)
from app.core.exceptions import BusinessException
from app.db.base import Base, user_roles
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.models.message import AiMessage
from app.modules.ai.models.operation_log import AiOperationLog
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.setting import SystemSetting
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions as transactions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


@pytest.fixture
async def resource_sessions(plan_sessions):
    _, plain = plan_sessions
    tables = [
        Dept.__table__,
        AiConversation.__table__,
        AiMessage.__table__,
        AiPreparedAction.__table__,
        AiOperationLog.__table__,
        SystemSetting.__table__,
        user_roles,
    ]
    async with plain.kw["bind"].begin() as connection:
        await connection.run_sync(
            lambda sync: Base.metadata.create_all(sync, tables=tables)
        )
    return plan_sessions


async def test_complete_plan_locks_real_context_domain_string_and_composite_keys(
    resource_sessions,
):
    factory, plain = resource_sessions
    async with plain() as db:
        await db.execute(
            insert(AiConversation.__table__).values(
                tenant_id=0, conversation_id=301, user_id=101
            )
        )
        await db.execute(
            insert(AiMessage.__table__).values(
                tenant_id=0, message_id=401, conversation_id=301, role="user"
            )
        )
        await db.execute(
            insert(AiPreparedAction.__table__).values(
                tenant_id=0,
                action_id=501,
                confirmation_id="plan-confirmation",
                interaction_flow="prepared",
                requested_outcome="execute",
                approval_mode="human",
                dispatch_mode="sync",
                execute_tool_call_id="plan-call",
                execute_tool_name="system_user_update",
                frozen_args={},
                args_hash="a" * 64,
                snapshot_hash="b" * 64,
                presentation={},
                user_id=101,
                conversation_id=301,
                source_user_message_id=401,
                trace_id="plan-trace",
                agent_code="plan-agent",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        await db.execute(
            insert(AiOperationLog.__table__).values(
                tenant_id=0,
                log_id=601,
                trace_id="plan-trace",
                conversation_id=301,
                source_user_message_id=401,
                user_id=101,
                tool_name="system_user_update",
                tool_call_id="plan-call",
                args_hash="a" * 64,
                args_summary="planned update",
                risk_level="high",
                execution_mode="hitl",
                status="pending_confirmation",
            )
        )
        await db.execute(
            insert(Dept.__table__).values(tenant_id=0, dept_id=701, dept_name="plan")
        )
        await db.execute(
            insert(Role.__table__).values(
                tenant_id=0,
                role_id=203,
                role_name="second",
                role_code="SECOND",
                status="1",
                data_scope="1",
            )
        )
        await db.execute(
            insert(SystemSetting.__table__),
            [
                {"tenant_id": 0, "setting_key": key, "setting_value": "enabled"}
                for key in ("z-plan", "a-plan", "A-plan")
            ],
        )
        await db.execute(
            insert(user_roles),
            [{"tenant_id": 0, "user_id": 101, "role_id": role} for role in (203, 201)],
        )
        await db.commit()

    keys = (
        ResourceKey(ResourceKind.USER_ROLES, 0, (101, 203)),
        ResourceKey(ResourceKind.SETTING, 0, "z-plan"),
        ResourceKey(ResourceKind.USER, 0, 101),
        ResourceKey(ResourceKind.DEPT, 0, 701),
        ResourceKey(ResourceKind.ROLE, 0, 203),
        ResourceKey(ResourceKind.OPERATION_LOG, 0, 601),
        ResourceKey(ResourceKind.PREPARED_ACTION, 0, 501),
        ResourceKey(ResourceKind.SOURCE_MESSAGE, 0, 401),
        ResourceKey(ResourceKind.CONVERSATION, 0, 301),
        ResourceKey(ResourceKind.ROLE, 0, 201),
        ResourceKey(ResourceKind.SETTING, 0, "a-plan"),
        ResourceKey(ResourceKind.SETTING, 0, "A-plan"),
        ResourceKey(ResourceKind.USER_ROLES, 0, (101, 201)),
    )
    observed = []
    executed = []

    def record(_conn, _cursor, sql, _params, context, _many):
        executed.append(sql)
        if "FOR UPDATE" in sql:
            statement = context.compiled.statement
            observed.append((statement.get_final_froms()[0].name, sql))

    event.listen(plain.kw["bind"].sync_engine, "before_cursor_execute", record)
    try:
        async with factory() as db:
            async with transactions.scope(db, tenants=(TenantLock(0),)) as tx:
                await tx.lock(LockPlan(existing=keys))
                before_assertion = len(executed)
                tx.assert_held(*keys)
                assert len(executed) == before_assertion
            await db.commit()
    finally:
        event.remove(plain.kw["bind"].sync_engine, "before_cursor_execute", record)

    assert [name for name, _ in observed] == [
        "ai_conversation",
        "ai_message",
        "ai_prepared_action",
        "ai_operation_log",
        "sys_role",
        "sys_dept",
        "sys_user",
        "sys_setting",
        "sys_user_role",
    ]
    setting_sql = observed[-2][1]
    assert 'ORDER BY sys_setting.setting_key COLLATE "C"' in setting_sql
    relation_sql = observed[-1][1]
    assert "ORDER BY sys_user_role.user_id, sys_user_role.role_id" in relation_sql
    assert "sys_user_role.tenant_id =" in relation_sql


async def test_reserved_tenant_is_rejected_before_structural_creation(plan_sessions):
    factory, plain = plan_sessions
    key = ResourceKey(ResourceKind.TENANT, None, 2)
    async with factory() as db:
        with pytest.raises(BusinessException) as error:
            async with transactions.scope_global(db) as tx:
                await tx.lock(LockPlan(new=(NewResource(key),)))
        assert error.value.error_code == "FIELD_POLICY_LOCK_ORDER"
        with pytest.raises(BusinessException):
            await db.commit()
        await db.rollback()
    async with plain() as db:
        assert (
            await db.scalar(select(Tenant.tenant_id).where(Tenant.tenant_id == 2))
            is None
        )
        assert (
            await db.scalar(
                select(UserFieldPolicyState.tenant_id).where(
                    UserFieldPolicyState.tenant_id == 2
                )
            )
            is None
        )


async def test_reserved_setting_rejects_unreserved_actual_primary_key(
    resource_sessions,
):
    factory, plain = resource_sessions
    key = ResourceKey(ResourceKind.SETTING, 0, "planned-setting")
    async with factory() as db:
        with pytest.raises(BusinessException) as error:
            async with transactions.scope(
                db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
            ) as tx:
                await tx.lock(LockPlan(new=(NewResource(key),)))
                await tx.insert_reserved(
                    key, {"setting_id": 801, "setting_value": "enabled"}
                )
        assert error.value.error_code == "FIELD_POLICY_LOCK_ORDER"
        with pytest.raises(BusinessException):
            await db.commit()
        await db.rollback()
    async with plain() as db:
        assert (await db.scalars(select(SystemSetting.setting_id))).all() == []


async def test_reserved_setting_owns_unique_key_with_generated_primary_key(
    resource_sessions,
):
    factory, plain = resource_sessions
    key = ResourceKey(ResourceKind.SETTING, 0, "planned-setting")
    setting = SystemSetting.__table__
    observed = []

    def record(_conn, _cursor, sql, _params, _context, _many):
        if sql.startswith("INSERT INTO sys_setting"):
            observed.append(sql)

    event.listen(plain.kw["bind"].sync_engine, "before_cursor_execute", record)
    try:
        async with factory() as db:
            async with transactions.scope(
                db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
            ) as tx:
                await tx.lock(LockPlan(new=(NewResource(key),)))
                await tx.insert_reserved(key, {"setting_value": "enabled"})
                tx.assert_held(key)
                generated_id = await db.scalar(
                    select(setting.c.setting_id).where(
                        setting.c.tenant_id == 0,
                        setting.c.setting_key == "planned-setting",
                    )
                )
                assert type(generated_id) is int and generated_id > 0
            await db.commit()
    finally:
        event.remove(plain.kw["bind"].sync_engine, "before_cursor_execute", record)
    assert len(observed) == 1
    assert "RETURNING sys_setting.setting_key" in observed[0]
    async with plain() as db:
        rows = (
            await db.execute(
                select(
                    setting.c.tenant_id,
                    setting.c.setting_key,
                    setting.c.setting_id,
                    setting.c.setting_value,
                )
            )
        ).all()
        assert rows == [(0, "planned-setting", generated_id, "enabled")]
