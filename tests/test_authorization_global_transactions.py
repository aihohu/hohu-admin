"""Global/structural typed mutations against complete disposable PostgreSQL DDL."""

import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.authorization_plan import (
    LockMode,
    LockPlan,
    NewResource,
    ResourceKey,
    ResourceKind,
    TenantLock,
)
from app.core.exceptions import BusinessException
from app.db.authorization_plan_session import AuthorizationPlanSession
from app.db.session import engine
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.provider import AiProvider
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.platform.models import PlatformAuditLog, PlatformPrincipal
from app.modules.system.models.mail_config import MailConfig
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.models.role import Role
from app.modules.system.models.setting import SystemSetting
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyGlobalGuard,
    UserFieldPolicyState,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions as transactions,
)
from tests.committed_database import isolated_migrated_schema

PROVIDER = ResourceKey(ResourceKind.PROVIDER, None, 101)
PRINCIPAL = ResourceKey(ResourceKind.PLATFORM_PRINCIPAL, None, 103)
MAIL = ResourceKey(ResourceKind.MAIL_CONFIG, None, 1)
TENANT0 = ResourceKey(ResourceKind.TENANT, None, 0)
TENANT1 = ResourceKey(ResourceKind.TENANT, None, 1)
SECURITY = ResourceKey(ResourceKind.SETTING, 0, "security:api_per_minute")


@pytest.fixture
async def global_sessions():
    async with isolated_migrated_schema(engine.url) as scoped:
        plain = async_sessionmaker(scoped, expire_on_commit=False, autoflush=False)
        managed = async_sessionmaker(
            scoped,
            class_=AuthorizationPlanSession,
            expire_on_commit=False,
            autoflush=False,
        )
        async with plain() as db:
            await db.execute(
                insert(Tenant).values(
                    tenant_id=1,
                    tenant_code="global-one",
                    tenant_name="disabled without bindings",
                    status="2",
                    lifecycle_state="prepared",
                    bootstrap_version=0,
                )
            )
            await db.execute(
                insert(AiProvider).values(
                    provider_id=101,
                    provider_code="global-test",
                    name="before",
                    api_key="fixture",
                )
            )
            await db.execute(
                insert(PlatformPrincipal).values(
                    principal_id=103,
                    principal_name="global-review",
                    display_name="review",
                    hashed_password="fixture",
                    permissions=["platform:tenant:read"],
                )
            )
            await db.execute(
                update(MailConfig)
                .where(MailConfig.config_id == 1)
                .values(values={}, version=1)
            )
            await db.execute(
                insert(SystemSetting).values(
                    tenant_id=0,
                    setting_key="security:api_per_minute",
                    setting_value="60",
                )
            )
            await db.execute(
                update(UserFieldPolicyState).values(
                    mode="ready", epoch=2, candidate_manifest_digest="a" * 64
                )
            )
            await db.commit()
        yield managed, plain


async def epochs(db):
    guard = await db.scalar(select(UserFieldPolicyGlobalGuard.epoch))
    states = (
        await db.execute(
            select(
                UserFieldPolicyState.tenant_id,
                UserFieldPolicyState.epoch,
                UserFieldPolicyState.mode,
                UserFieldPolicyState.candidate_manifest_digest,
            ).order_by(UserFieldPolicyState.tenant_id)
        )
    ).all()
    return guard, [tuple(row) for row in states]


def tenant_scope(db, tenant_id=0):
    return transactions.scope(db, tenants=(TenantLock(tenant_id, LockMode.EXCLUSIVE),))


@pytest.mark.parametrize(
    ("key", "changes"),
    [
        (PROVIDER, {"name": "after"}),
        (SECURITY, {"setting_value": "61"}),
        (PRINCIPAL, {"permissions": ["platform:tenant:write"]}),
        (MAIL, {"values": {"host": "changed"}, "version": 2}),
    ],
)
async def test_global_actual_change_invalidates_every_tenant_once(
    global_sessions, key, changes
):
    managed, plain = global_sessions
    async with managed() as db:
        async with transactions.scope_global(db) as tx:
            await tx.lock(LockPlan(existing=(key,)))
            async with tx.aggregate_global():
                assert await tx.update_held(key, changes)
                assert not await tx.update_held(key, changes)
        await db.commit()
    async with plain() as db:
        assert await epochs(db) == (
            2,
            [(0, 3, "legacy", "a" * 64), (1, 3, "legacy", "a" * 64)],
        )
        if key == PRINCIPAL:
            assert await db.scalar(select(PlatformPrincipal.row_version)) == 2


async def test_global_noop_and_rolled_back_savepoint_preserve_ready(global_sessions):
    managed, plain = global_sessions
    async with managed() as db:
        async with transactions.scope_global(db) as tx:
            await tx.lock(LockPlan(existing=(PROVIDER,)))
            async with tx.aggregate_global():
                assert not await tx.update_held(PROVIDER, {"name": "before"})
                with pytest.raises(ValueError):
                    async with tx.savepoint():
                        assert await tx.update_held(PROVIDER, {"name": "undone"})
                        raise ValueError("rollback this child")
        await db.commit()
    async with plain() as db:
        assert await epochs(db) == (
            1,
            [(0, 2, "ready", "a" * 64), (1, 2, "ready", "a" * 64)],
        )
        assert await db.scalar(select(AiProvider.name)) == "before"


async def test_global_failure_rolls_back_business_and_all_epochs(global_sessions):
    managed, plain = global_sessions
    async with managed() as db:
        with pytest.raises(ValueError):
            async with transactions.scope_global(db) as tx:
                await tx.lock(LockPlan(existing=(PROVIDER,)))
                async with tx.aggregate_global():
                    await tx.update_held(PROVIDER, {"name": "undone"})
                raise ValueError("mandatory audit failed")
        with pytest.raises(BusinessException):
            await db.commit()
        await db.rollback()
    async with plain() as db:
        assert (await epochs(db))[0] == 1
        assert await db.scalar(select(AiProvider.name)) == "before"


async def test_global_read_held_is_allowed_but_write_is_not(global_sessions):
    managed, _ = global_sessions
    async with managed() as db:
        async with tenant_scope(db) as tx:
            await tx.lock(LockPlan(existing=(PROVIDER,)))
            tx.assert_held(PROVIDER)
        await db.rollback()
        with pytest.raises(BusinessException):
            async with tenant_scope(db) as tx:
                await tx.lock(LockPlan(existing=(PROVIDER,)))
                async with tx.aggregate(0):
                    await tx.update_held(PROVIDER, {"name": "forbidden"})
        await db.rollback()


async def test_global_aggregate_requires_all_tenants_proof(global_sessions):
    managed, _ = global_sessions
    async with managed() as db:
        with pytest.raises(BusinessException):
            async with transactions.scope(
                db,
                tenants=(TenantLock(0, LockMode.EXCLUSIVE),),
                global_mode=LockMode.EXCLUSIVE,
            ) as tx:
                await tx.lock(LockPlan(existing=(PROVIDER,)))
                async with tx.aggregate_global():
                    await tx.update_held(PROVIDER, {"name": "forbidden"})
        await db.rollback()


@pytest.mark.parametrize("inside_frame", [False, True])
async def test_global_insert_requires_frame_and_records_actual_change(
    global_sessions, inside_frame
):
    managed, plain = global_sessions
    key = ResourceKey(ResourceKind.PROVIDER, None, 104)
    values = {"provider_code": "inserted", "name": "inserted", "api_key": "fixture"}
    async with managed() as db:
        if inside_frame:
            async with transactions.scope_global(db) as tx:
                await tx.lock(LockPlan(new=(NewResource(key),)))
                async with tx.aggregate_global():
                    await tx.insert_reserved(key, values)
            await db.commit()
        else:
            with pytest.raises(BusinessException):
                async with transactions.scope_global(db) as tx:
                    await tx.lock(LockPlan(new=(NewResource(key),)))
                    await tx.insert_reserved(key, values)
            await db.rollback()
    async with plain() as db:
        assert (await epochs(db))[0] == (2 if inside_frame else 1)


async def test_security_setting_cannot_be_written_as_tenant_fact(global_sessions):
    managed, _ = global_sessions
    async with managed() as db:
        with pytest.raises(BusinessException):
            async with tenant_scope(db) as tx:
                await tx.lock(LockPlan(existing=(SECURITY,)))
                async with tx.aggregate(0):
                    await tx.update_held(SECURITY, {"setting_value": "bad"})
        await db.rollback()


async def test_target_tenant_update_bumps_only_target_and_trigger_version(
    global_sessions,
):
    managed, plain = global_sessions
    async with managed() as db:
        async with tenant_scope(db, 1) as tx:
            await tx.lock(LockPlan(existing=(TENANT1,)))
            async with tx.aggregate(1):
                await tx.update_held(TENANT1, {"lifecycle_state": "disabled"})
        await db.commit()
    async with plain() as db:
        assert await epochs(db) == (
            1,
            [(0, 2, "ready", "a" * 64), (1, 3, "legacy", "a" * 64)],
        )
        assert (
            await db.scalar(select(Tenant.row_version).where(Tenant.tenant_id == 1))
            == 2
        )


@pytest.mark.parametrize(
    "key,changes",
    [
        (PRINCIPAL, {"last_login_at": datetime(2026, 10, 27, tzinfo=UTC)}),
        (MAIL, {"last_test_status": "success", "version": 2}),
    ],
)
async def test_metadata_is_fixed_and_does_not_invalidate(global_sessions, key, changes):
    managed, plain = global_sessions
    async with managed() as db:
        async with transactions.scope(db) as tx:
            await tx.lock(LockPlan(existing=(key,)))
            expected = {"version": 1} if key == MAIL else {"row_version": 1}
            assert await tx.update_metadata(key, changes, expected=expected)
        await db.commit()
    async with plain() as db:
        assert (await epochs(db))[0] == 1
        assert all(row[1] == 2 for row in (await epochs(db))[1])
        assert await db.scalar(select(PlatformPrincipal.row_version)) == 1


@pytest.mark.parametrize(
    "key,changes",
    [
        (PRINCIPAL, {"permissions": []}),
        (PRINCIPAL, {"row_version": 99}),
        (MAIL, {"password_ciphertext": "forbidden"}),
        (MAIL, {"version": 99}),
    ],
)
async def test_metadata_cannot_bypass_security_or_version_cas(
    global_sessions, key, changes
):
    managed, _ = global_sessions
    async with managed() as db:
        with pytest.raises(BusinessException):
            async with transactions.scope(db) as tx:
                await tx.lock(LockPlan(existing=(key,)))
                await tx.update_metadata(
                    key, changes, expected={"version": 1} if key == MAIL else {}
                )
        await db.rollback()


def tenant_values():
    return {
        "tenant_code": "created-two",
        "tenant_name": "created",
        "status": "2",
        "lifecycle_state": "prepared",
        "provisioning_key_hash": "c" * 64,
        "provisioning_fingerprint": "d" * 64,
    }


async def test_structure_create_locks_triggered_state_and_preserves_old_tenants(
    global_sessions,
):
    managed, plain = global_sessions
    key = ResourceKey(ResourceKind.TENANT, None, 2)
    async with managed() as db:
        async with transactions.scope_creation(db) as tx:
            await tx.create_tenant(key, tenant_values())
            await tx.lock(LockPlan())
            tx.assert_held(key)
        await db.commit()
    async with plain() as db:
        assert await epochs(db) == (
            2,
            [
                (0, 2, "ready", "a" * 64),
                (1, 2, "ready", "a" * 64),
                (2, 1, "legacy", None),
            ],
        )


async def test_structure_replay_uses_existing_complete_barriers_without_epoch(
    global_sessions,
):
    managed, plain = global_sessions
    async with managed() as db:
        async with transactions.scope_creation(db) as tx:
            await tx.replay_tenants((TenantLock(0), TenantLock(1, LockMode.EXCLUSIVE)))
            await tx.lock(LockPlan(existing=(TENANT0, TENANT1)))
        await db.commit()
    async with plain() as db:
        assert (await epochs(db))[0] == 1


@pytest.mark.parametrize("violation", ["second", "after_plan", "arbitrary_state"])
async def test_structural_exception_does_not_expand(global_sessions, violation):
    managed, plain = global_sessions
    key = ResourceKey(ResourceKind.TENANT, None, 2)
    async with managed() as db:
        with pytest.raises(BusinessException):
            async with transactions.scope_creation(db) as tx:
                if violation == "arbitrary_state":
                    await tx.create_tenant(key, tenant_values() | {"row_version": 99})
                elif violation == "after_plan":
                    await tx.lock(LockPlan())
                    await tx.create_tenant(key, tenant_values())
                else:
                    await tx.create_tenant(key, tenant_values())
                    await tx.create_tenant(
                        ResourceKey(ResourceKind.TENANT, None, 3), tenant_values()
                    )
        await db.rollback()
    async with plain() as db:
        assert (
            await db.scalar(select(Tenant.tenant_id).where(Tenant.tenant_id == 2))
            is None
        )


def operation_values(*, scope="platform"):
    return {
        "audit_scope": scope,
        "user_id": 999,
        "username": "actor-snapshot",
        "module": "test",
        "action": "completed",
        "method": "POST",
        "path": "/platform/test",
        "status_code": 200,
    }


async def test_shared_scope_audit_requires_real_parent_and_does_not_bump(
    global_sessions,
):
    managed, plain = global_sessions
    audit = ResourceKey(ResourceKind.SYSTEM_OPERATION_LOG, 0, 501)
    async with managed() as db:
        async with transactions.scope(db, tenants=(TenantLock(0),)) as tx:
            await tx.lock(LockPlan(existing=(TENANT0,), new=(NewResource(audit),)))
            assert await tx.append_audit(audit, operation_values()) == 501
        await db.commit()
    async with plain() as db:
        assert await db.scalar(select(SysOperationLog.user_id)) == 999
        assert (await epochs(db))[0] == 1


async def test_audit_without_parent_is_rejected(global_sessions):
    managed, _ = global_sessions
    audit = ResourceKey(ResourceKind.SYSTEM_OPERATION_LOG, 0, 501)
    async with managed() as db:
        with pytest.raises(BusinessException):
            async with transactions.scope(db, tenants=(TenantLock(0),)) as tx:
                await tx.lock(LockPlan(new=(NewResource(audit),)))
                await tx.append_audit(audit, operation_values())
        await db.rollback()


def platform_values(*, event_type="authorized", authorization=None, status=None):
    return {
        "actor_principal_id": 103,
        "actor_name": "global-review",
        "permission": "platform:tenant:read",
        "event_type": event_type,
        "method": "GET",
        "path": "/platform/test",
        "reason": "review",
        "ticket_id": "case-1",
        "correlation_id": "correlation-1",
        "authorization_audit_id": authorization,
        "status_code": status,
        "duration_ms": 1 if event_type == "completed" else None,
        "result_summary": {"statusCode": status} if status else None,
    }


async def test_platform_append_preserves_lineage_and_completion_replay(global_sessions):
    managed, plain = global_sessions
    authorized = ResourceKey(ResourceKind.PLATFORM_AUDIT, None, 601)
    async with managed() as db:
        async with transactions.scope(db) as tx:
            await tx.lock(
                LockPlan(existing=(PRINCIPAL,), new=(NewResource(authorized),))
            )
            assert await tx.append_audit(authorized, platform_values()) == 601
        await db.commit()
    for audit_id in (602, 603):
        completion = ResourceKey(ResourceKind.PLATFORM_AUDIT, None, audit_id)
        async with managed() as db:
            async with transactions.scope(db) as tx:
                await tx.lock(
                    LockPlan(
                        existing=(PRINCIPAL, authorized), new=(NewResource(completion),)
                    )
                )
                assert (
                    await tx.append_audit(
                        completion,
                        platform_values(
                            event_type="completed", authorization=601, status=200
                        ),
                    )
                    == 602
                )
            await db.commit()
    async with plain() as db:
        assert (
            await db.scalars(
                select(PlatformAuditLog.audit_id).order_by(PlatformAuditLog.audit_id)
            )
        ).all() == [601, 602]


@pytest.mark.parametrize("scope", ["tenant", "platform"])
async def test_retention_never_deletes_platform_audit_or_bumps_epoch(
    global_sessions, scope
):
    managed, plain = global_sessions
    key = ResourceKey(ResourceKind.SYSTEM_OPERATION_LOG, 0, 701)
    async with plain() as db:
        await db.execute(
            insert(SysOperationLog).values(
                operation_log_id=701, tenant_id=0, **operation_values(scope=scope)
            )
        )
        await db.commit()
    async with managed() as db:
        if scope == "tenant":
            async with tenant_scope(db) as tx:
                await tx.lock(LockPlan(existing=(key,)))
                assert await tx.delete_retained(key, expected={"audit_scope": "tenant"})
            await db.commit()
        else:
            with pytest.raises(BusinessException):
                async with tenant_scope(db) as tx:
                    await tx.lock(LockPlan(existing=(key,)))
                    await tx.delete_retained(key)
            await db.rollback()
    async with plain() as db:
        assert (await epochs(db))[0] == 1
        assert (await db.scalar(select(SysOperationLog.operation_log_id))) == (
            701 if scope == "platform" else None
        )


@pytest.mark.parametrize("first", ["create", "global"])
async def test_creation_and_global_update_serialize_in_both_orders(
    global_sessions, first
):
    managed, plain = global_sessions
    key = ResourceKey(ResourceKind.TENANT, None, 2)
    attempted = asyncio.Event()

    async def create(db):
        async with transactions.scope_creation(db) as tx:
            await tx.create_tenant(key, tenant_values())
            await tx.lock(LockPlan())

    async def global_change(db):
        async with transactions.scope_global(db) as tx:
            await tx.lock(LockPlan(existing=(PROVIDER,)))
            async with tx.aggregate_global():
                await tx.update_held(PROVIDER, {"name": "latest"})

    async def second_writer():
        async with managed() as db:
            attempted.set()
            await (global_change(db) if first == "create" else create(db))
            await db.commit()

    async with managed() as db:
        await (create(db) if first == "create" else global_change(db))
        contender = asyncio.create_task(second_writer())
        try:
            await attempted.wait()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(contender), 0.12)
            await db.commit()
            await asyncio.wait_for(contender, 3)
        finally:
            if not contender.done():
                contender.cancel()
                await asyncio.gather(contender, return_exceptions=True)
    async with plain() as db:
        guard, states = await epochs(db)
        assert guard == 3
        assert states[:2] == [(0, 3, "legacy", "a" * 64), (1, 3, "legacy", "a" * 64)]
        assert states[2] == (2, 2 if first == "create" else 1, "legacy", None)


@pytest.mark.parametrize("corruption", ["missing_trigger", "missing_existing_b"])
async def test_creation_and_replay_do_not_repair_missing_states(
    global_sessions, corruption
):
    managed, plain = global_sessions
    async with plain() as db:
        if corruption == "missing_trigger":
            await db.execute(
                text("DROP TRIGGER trg_ufp_initialize_tenant ON sys_tenant")
            )
        else:
            await db.execute(text("TRUNCATE sys_user_field_policy_state"))
        await db.commit()
    async with managed() as db:
        with pytest.raises(BusinessException) as caught:
            async with transactions.scope_creation(db) as tx:
                if corruption == "missing_trigger":
                    await tx.create_tenant(
                        ResourceKey(ResourceKind.TENANT, None, 2), tenant_values()
                    )
                else:
                    await tx.replay_tenants((TenantLock(1, LockMode.EXCLUSIVE),))
        assert caught.value.error_code == "FIELD_POLICY_UNAVAILABLE"
        await db.rollback()
    async with plain() as db:
        assert (
            await db.scalar(select(Tenant.tenant_id).where(Tenant.tenant_id == 2))
            is None
        )


@pytest.mark.parametrize("count", [1023, 1024])
async def test_creation_capacity_includes_all_statuses_but_replay_is_not_new(
    global_sessions, count
):
    managed, plain = global_sessions
    async with plain() as db:
        await db.execute(
            insert(Tenant),
            [
                {
                    "tenant_id": i,
                    "tenant_code": f"capacity-{i}",
                    "tenant_name": "disabled",
                    "status": "2",
                    "lifecycle_state": "disabled",
                    "bootstrap_version": 0,
                }
                for i in range(2, count)
            ],
        )
        await db.commit()
    async with managed() as db:
        if count == 1023:
            async with transactions.scope_creation(db) as tx:
                await tx.create_tenant(
                    ResourceKey(ResourceKind.TENANT, None, 2000), tenant_values()
                )
                await tx.lock(LockPlan())
            await db.commit()
        else:
            with pytest.raises(BusinessException) as caught:
                async with transactions.scope_creation(db) as tx:
                    await tx.create_tenant(
                        ResourceKey(ResourceKind.TENANT, None, 2000), tenant_values()
                    )
            assert caught.value.error_code == "AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
            await db.rollback()
        async with transactions.scope_creation(db) as tx:
            await tx.replay_tenants((TenantLock(1, LockMode.EXCLUSIVE),))
            await tx.lock(LockPlan(existing=(TENANT1,)))
        await db.commit()


async def test_structural_source_audit_is_narrow_and_atomic(global_sessions):
    managed, plain = global_sessions
    audit = ResourceKey(ResourceKind.SYSTEM_OPERATION_LOG, 0, 801)
    async with managed() as db:
        async with transactions.scope_creation(db) as tx:
            await tx.create_tenant(
                ResourceKey(ResourceKind.TENANT, None, 2), tenant_values()
            )
            await tx.lock(LockPlan(existing=(TENANT0,), new=(NewResource(audit),)))
            await tx.append_audit(audit, operation_values())
        await db.commit()
    async with plain() as db:
        assert (await epochs(db))[0] == 2
        assert await db.scalar(select(SysOperationLog.operation_log_id)) == 801


async def test_role_agent_binding_requires_and_uses_complete_parents(global_sessions):
    managed, plain = global_sessions
    role = ResourceKey(ResourceKind.ROLE, 0, 201)
    agent = ResourceKey(ResourceKind.AGENT, None, 301)
    link = ResourceKey(ResourceKind.ROLE_AGENT, 0, (201, 301))
    async with plain() as db:
        await db.execute(
            insert(Role).values(
                tenant_id=0, role_id=201, role_code="BIND", role_name="bind", status="1"
            )
        )
        await db.execute(
            insert(AiAgent).values(
                agent_id=301, code="bind", name="bind", description="fixture"
            )
        )
        await db.commit()
    async with managed() as db:
        async with tenant_scope(db) as tx:
            await tx.lock(LockPlan(existing=(role, agent), new=(NewResource(link),)))
            async with tx.aggregate(0):
                await tx.insert_reserved(link, {"enabled": True})
                await tx.update_held(link, {"enabled": False})
        await db.commit()
    async with plain() as db:
        assert await db.scalar(select(RoleAiAgent.enabled)) is False
        assert (await epochs(db))[1] == [
            (0, 3, "legacy", "a" * 64),
            (1, 2, "ready", "a" * 64),
        ]


async def test_structural_protocol_cannot_be_entered_after_an_advisory_lock(
    global_sessions,
):
    managed, _ = global_sessions
    async with managed() as db:
        await db.execute(text("SELECT pg_advisory_xact_lock(12702023)"))
        with pytest.raises(BusinessException):
            async with transactions.scope_creation(db):
                pass
        await db.rollback()
