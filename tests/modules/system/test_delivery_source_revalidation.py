"""Old receipts must prove current per-role fields, not just union visibility."""

import pytest
from sqlalchemy import delete, insert, select

from app.core.authorization_plan import TenantLock
from app.core.exceptions import BusinessException
from app.core.field_policy import FieldRef, Grant
from app.core.tenant import TenantContext
from app.db.authorization_runtime import AuthorizationRuntime
from app.db.base import role_menus, user_roles
from app.modules.ai.service.delivery_source_domains import source_domain, with_sources
from app.modules.ai.service.delivery_source_revalidation import revalidate_sources
from app.modules.ai.service.field_authority_runtime import ai_field_authority
from app.modules.system.field_policy.delivery_catalog import READ
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
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


async def setup_source(sessions, monkeypatch):
    _, plain = sessions
    async with plain() as db:
        await db.execute(
            insert(Role).values(
                role_id=203,
                tenant_id=0,
                role_name="Second",
                role_code="SECOND",
                data_scope="1",
                status="1",
            )
        )
        await db.execute(
            insert(User).values(
                user_id=105,
                tenant_id=0,
                user_name="other_admin",
                hashed_password="unusable-fixture",
                status="1",
            )
        )
        await db.execute(
            insert(user_roles).values(tenant_id=0, user_id=105, role_id=201)
        )
        await db.execute(
            insert(Menu).values(
                menu_id=20001,
                tenant_id=0,
                menu_name="Chat",
                menu_type="F",
                permission="ai:chat:use",
                status="1",
            )
        )
        await db.execute(
            insert(role_menus).values(tenant_id=0, role_id=201, menu_id=20001)
        )
        await db.commit()
    await activate_relations(
        sessions,
        monkeypatch,
        extra_grants=(
            Grant(0, "203", FieldRef("system.role", "role_name"), READ, "deny"),
        ),
    )
    async with sessions[0]() as db:
        async with authorization_transactions.scope(db, tenants=(TenantLock(0),)) as tx:
            stamp = await ai_field_authority.current(db, tx, tenant=TENANT)
            value = with_sources(
                stamp,
                (
                    source_domain(
                        action_id="role.list",
                        object_id="system.role",
                        records=(201,),
                        read=(("role_name", "plain"),),
                    ),
                ),
            )
        await db.rollback()
    return value


async def test_renewed_delivery_rechecks_each_applicable_role_after_owned_role_change(
    system_sessions, monkeypatch
):

    frozen = await setup_source(system_sessions, monkeypatch)
    runtime = AuthorizationRuntime(system_sessions[1].kw["bind"])
    await runtime.system(
        tenant=TenantContext(0, "plan-0", 105, 1, "access_token"),
        actor_user_version=1,
        operation="user.roles",
        payload=[201, 203],
        target_id=101,
    )
    async with system_sessions[1]() as db:
        version = await db.scalar(select(User.auth_version).where(User.user_id == 101))
    async with system_sessions[0]() as db:
        async with authorization_transactions.scope(db, tenants=(TenantLock(0),)) as tx:
            with pytest.raises(BusinessException) as caught:
                await revalidate_sources(
                    db, tx, tenant=TENANT, actor_user_version=version, receipt=frozen
                )
            assert caught.value.error_code == "FIELD_AUTHORIZATION_CHANGED"
        await db.rollback()


async def test_query_receipt_does_not_lose_its_original_records_after_scope_or_deletion(
    system_sessions, monkeypatch
):

    frozen = await setup_source(system_sessions, monkeypatch)
    async with system_sessions[1]() as db:
        await db.execute(
            insert(Role).values(
                role_id=204,
                tenant_id=0,
                role_name="Temporary source",
                role_code="TEMP_SOURCE",
                data_scope="1",
                status="1",
            )
        )
        await db.commit()
    authority = frozen["authority"]
    missing = with_sources(
        authority,
        (
            source_domain(
                action_id="role.list",
                object_id="system.role",
                records=(204,),
                query=(("status", "eq", "management.query", "filter"),),
            ),
        ),
    )
    async with system_sessions[0]() as db:
        async with authorization_transactions.scope(db, tenants=(TenantLock(0),)) as tx:
            try:
                await revalidate_sources(
                    db, tx, tenant=TENANT, actor_user_version=1, receipt=missing
                )
            except BusinessException as error:
                raise AssertionError(
                    getattr(error.__context__, "error_code", "SOURCE_PROOF_FAILED")
                ) from None
        await db.rollback()
    async with system_sessions[1]() as db:
        await db.execute(delete(Role).where(Role.role_id == 204))
        await db.commit()
    async with system_sessions[0]() as db:
        async with authorization_transactions.scope(db, tenants=(TenantLock(0),)) as tx:
            with pytest.raises(BusinessException):
                await revalidate_sources(
                    db, tx, tenant=TENANT, actor_user_version=1, receipt=missing
                )
        await db.rollback()
