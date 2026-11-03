"""Real barriers, immutable manifests and irreversible active policy state."""

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from app.core.authorization_plan import LockMode, TenantLock
from app.core.config import settings
from app.core.exceptions import BusinessException
from app.db.authorization_runtime import AuthorizationRuntime
from app.db.base import role_menus, user_roles
from app.modules.ai.service.field_authority_runtime import ai_field_authority
from app.modules.system.field_policy.evaluator import FieldGrant
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import UserFieldPolicyState as State
from app.modules.system.schemas.user import UserPatch, UserQuery
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions as transactions,
)
from app.modules.system.service.planned_user_field_policy_service import (
    planned_user_field_policy_service as policies,
)
from app.modules.system.service.user_ai_fields import user_ai_fields
from app.modules.system.service.user_field_active import active_policy_grants
from app.modules.system.service.user_transfer_fields import UserTransferFields
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_planned_user_field_policy import TENANT, change
from tests.modules.system.test_planned_user_field_policy import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_planned_user_field_policy import (
    policy_sessions as policy_sessions,
)


async def transition(factory, action, state):
    async with factory() as db:
        async with transactions.scope(
            db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
        ) as tx:
            plan = await policies.plan(db, tx, tenant=TENANT, action=action)
            await tx.lock(plan.lock_plan)
            result = await policies.transition(
                db,
                tx,
                plan,
                tenant=TENANT,
                expected_epoch=state.epoch,
                expected_manifest_digest=state.candidate_manifest_digest,
                reason="isolated acceptance",
                correlation_id="active-test",
            )
        await db.commit()
    return result


async def activate(factory):
    await change(
        factory,
        grants=(
            FieldGrant("201", "nickname", "read", "management.read"),
            FieldGrant("201", "create_time", "read", "management.read"),
        ),
        expected_version=0,
    )
    async with factory() as db:
        state = policies._state_snapshot(await policies._state(db, TENANT))
        await db.rollback()
    ready = await change(
        factory,
        action="ready",
        expected_epoch=state.epoch,
        expected_manifest_digest=state.candidate_manifest_digest,
    )
    return await transition(factory, "activate", ready)


async def read(factory, roles=("201",)):
    async with factory() as db:
        async with transactions.scope(db, tenants=(TenantLock(0),)) as tx:
            result = await active_policy_grants(db, tx, tenant_id=0, role_ids=roles)
        await db.rollback()
    return result


async def test_active_manifest_loads_only_current_roles_and_never_falls_back(
    policy_sessions, monkeypatch
):
    factory, _ = policy_sessions
    monkeypatch.setattr(settings, "USER_FIELD_POLICY_AVAILABLE", True)
    assert await read(factory) is None
    state = await activate(factory)
    assert state.mode == "enforcing" and state.ever_enforced
    assert {r.field_id for r in await read(factory)} == {"nickname", "create_time"}
    assert await read(factory, ("202",)) == ()
    monkeypatch.setattr(settings, "USER_FIELD_POLICY_AVAILABLE", False)
    with pytest.raises(BusinessException, match="") as exc:
        await read(factory)
    assert exc.value.error_code == "FIELD_POLICY_UNAVAILABLE"


async def test_freeze_publish_reactivate_keeps_security_state(
    policy_sessions, monkeypatch
):
    factory, _ = policy_sessions
    monkeypatch.setattr(settings, "USER_FIELD_POLICY_AVAILABLE", True)
    active = await activate(factory)
    frozen = await transition(factory, "freeze", active)
    with pytest.raises(BusinessException):
        await read(factory)
    await change(factory, grants=(), expected_version=1)
    async with factory() as db:
        state = policies._state_snapshot(await policies._state(db, TENANT))
        await db.rollback()
    assert state.mode == "frozen" and state.ever_enforced
    with pytest.raises(BusinessException):
        await change(
            factory,
            action="ready",
            expected_epoch=state.epoch,
            expected_manifest_digest=state.candidate_manifest_digest,
        )
    repaired = await transition(factory, "activate", state)
    assert repaired.epoch > frozen.epoch and repaired.mode == "enforcing"
    assert await read(factory) == ()


async def test_database_rejects_reversion_even_with_a_newer_epoch(
    policy_sessions, monkeypatch
):
    factory, plain = policy_sessions
    monkeypatch.setattr(settings, "USER_FIELD_POLICY_AVAILABLE", True)
    await activate(factory)
    async with plain() as db:
        with pytest.raises(IntegrityError):
            await db.execute(
                update(State)
                .where(State.tenant_id == 0)
                .values(
                    mode="legacy",
                    ever_enforced=False,
                    active_manifest_digest=None,
                    epoch=State.epoch + 1,
                )
            )
        await db.rollback()


async def test_active_publication_updates_active_digest_atomically(
    policy_sessions, monkeypatch
):
    factory, plain = policy_sessions
    monkeypatch.setattr(settings, "USER_FIELD_POLICY_AVAILABLE", True)
    active = await activate(factory)
    await change(factory, grants=(), expected_version=1)
    async with plain() as db:
        state = await db.scalar(select(State).where(State.tenant_id == 0))
        assert state.mode == "enforcing" and state.ever_enforced
        assert state.active_manifest_digest == state.candidate_manifest_digest
        assert state.active_manifest_digest != active.active_manifest_digest
        await db.execute(
            update(State)
            .where(State.tenant_id == 0)
            .values(
                active_manifest_digest="a" * 64,
                candidate_manifest_digest="a" * 64,
                epoch=State.epoch + 1,
            )
        )
        await db.commit()
    with pytest.raises(BusinessException) as exc:
        await read(factory)
    assert exc.value.error_code == "FIELD_POLICY_UNAVAILABLE"


async def test_production_list_and_old_write_do_not_bypass_active_policy(
    system_sessions, monkeypatch
):
    factory, plain = system_sessions
    monkeypatch.setattr(settings, "USER_FIELD_POLICY_AVAILABLE", True)
    await activate(factory)
    runtime = AuthorizationRuntime(plain.kw["bind"])
    response = await runtime.user_read(
        tenant=TENANT, actor_user_version=1, operation="list", query=UserQuery()
    )
    row = response["records"][0]
    assert "nickname" in row and "userName" not in row
    assert row["fieldAccess"]["userName"]["read"] == "hidden"
    with pytest.raises(BusinessException) as exc:
        await runtime.system(
            tenant=TENANT,
            actor_user_version=1,
            operation="user.update",
            target_id=101,
            payload=UserPatch(nickname="forbidden"),
        )
    assert exc.value.error_code == "FIELD_WRITE_DENIED"
    await change(
        factory,
        grants=(FieldGrant("201", "nickname", "update", "management.update"),),
        expected_version=1,
    )
    await runtime.system(
        tenant=TENANT,
        actor_user_version=1,
        operation="user.update",
        target_id=101,
        payload=UserPatch(nickname="allowed"),
    )
    async with plain() as db:
        assert (
            await db.scalar(select(State).where(State.tenant_id == 0))
        ).mode == "enforcing"


async def test_multi_role_deny_wins_without_borrowing_other_role_scope(
    system_sessions, monkeypatch
):
    factory, plain = system_sessions
    monkeypatch.setattr(settings, "USER_FIELD_POLICY_AVAILABLE", True)
    async with plain() as db:
        await db.execute(
            update(Role)
            .where(Role.role_id == 201)
            .values(role_code="SELF", data_scope="5")
        )
        await db.execute(
            insert(Menu).values(
                tenant_id=0,
                menu_id=3001,
                menu_name="list",
                menu_type="F",
                status="1",
                permission="system:user:list",
            )
        )
        await db.execute(
            insert(role_menus).values(tenant_id=0, role_id=201, menu_id=3001)
        )
        await db.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=301,
                role_code="ALL",
                role_name="All",
                status="1",
                data_scope="1",
            )
        )
        await db.execute(
            insert(user_roles).values(tenant_id=0, user_id=101, role_id=301)
        )
        await db.execute(
            insert(User).values(
                tenant_id=0,
                user_id=501,
                user_name="other",
                nickname="outside-self-scope",
                hashed_password="unused",
                status="1",
            )
        )
        await db.commit()
    await change(
        factory,
        role_id=301,
        grants=(FieldGrant("301", "create_time", "read", "management.read"),),
        expected_version=0,
    )
    await activate(factory)
    runtime = AuthorizationRuntime(plain.kw["bind"])

    async def rows():
        response = await runtime.user_read(
            tenant=TENANT, actor_user_version=1, operation="list", query=UserQuery()
        )
        return {r["userId"]: r for r in response["records"]}

    projected = await rows()
    assert "nickname" in projected["101"] and "nickname" not in projected["501"]
    await change(
        factory,
        role_id=301,
        grants=(
            FieldGrant("301", "create_time", "read", "management.read"),
            FieldGrant("301", "nickname", "read", "management.read", effect="deny"),
        ),
        expected_version=1,
    )
    projected = await rows()
    assert all("nickname" not in row for row in projected.values())
    with pytest.raises(BusinessException) as exc:
        await runtime.user_read(
            tenant=TENANT,
            actor_user_version=1,
            operation="list",
            query=UserQuery(nickname="outside"),
        )
    assert exc.value.error_code == "FIELD_QUERY_DENIED"


async def test_frozen_keeps_minimal_auth_but_blocks_profile_and_history(
    system_sessions, monkeypatch
):
    factory, plain = system_sessions
    monkeypatch.setattr(settings, "USER_FIELD_POLICY_AVAILABLE", True)
    state = await activate(factory)
    runtime = AuthorizationRuntime(plain.kw["bind"])
    async with factory() as db:
        async with transactions.scope(db, tenants=(TenantLock(0),)) as tx:
            watermark = await ai_field_authority.current(db, tx, tenant=TENANT)
            for adapter in (UserTransferFields(), user_ai_fields):
                context = await adapter.context(db, tx, tenant=TENANT, version=1)
                assert {r.field_id for r in context.grants} == {
                    "nickname",
                    "create_time",
                }
            with pytest.raises(BusinessException):
                await ai_field_authority.require(
                    db, tx, tenant=TENANT, frozen=None, legacy=True
                )
        await db.rollback()
    await transition(factory, "freeze", state)
    auth = await runtime.user_read(
        tenant=TENANT, actor_user_version=1, operation="auth"
    )
    assert auth["userId"] == "101" and "userAvatar" not in auth
    with pytest.raises(BusinessException):
        await runtime.user_read(
            tenant=TENANT, actor_user_version=1, operation="profile"
        )
    async with factory() as db:
        async with transactions.scope(db, tenants=(TenantLock(0),)) as tx:
            with pytest.raises(BusinessException):
                await ai_field_authority.require(
                    db, tx, tenant=TENANT, frozen=watermark
                )
        await db.rollback()


async def test_own_account_deny_and_cross_tenant_compatibility(
    system_sessions, monkeypatch
):
    factory, plain = system_sessions
    monkeypatch.setattr(settings, "USER_FIELD_POLICY_AVAILABLE", True)
    await activate(factory)
    await change(
        factory,
        grants=(
            FieldGrant("201", "user_email", "read", "own-account", effect="deny"),
            FieldGrant("201", "user_email", "update", "own-account", effect="deny"),
        ),
        expected_version=1,
    )
    runtime = AuthorizationRuntime(plain.kw["bind"])
    profile = await runtime.user_read(
        tenant=TENANT, actor_user_version=1, operation="profile"
    )
    assert "userEmail" not in profile
    assert not profile["fieldAccess"]["userEmail"]["update"]
    async with factory() as db:
        async with transactions.scope(db, tenants=(TenantLock(1),)) as tx:
            assert (
                await active_policy_grants(db, tx, tenant_id=1, role_ids=("201",))
                is None
            )
        await db.rollback()
