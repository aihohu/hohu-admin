"""A tenant created after generic rollout starts with denied, configurable policy state."""

import pytest
from sqlalchemy import insert, select

from app.core.authorization_plan import LockPlan, ResourceKey, ResourceKind, TenantLock
from app.core.config import settings
from app.core.exceptions import BusinessException
from app.core.tenant import TenantContext
from app.db.authorization_field_management_owner import FieldManagementOwner
from app.modules.system.field_policy.ordinary_catalog import production_catalog
from app.modules.system.models.field_policy import (
    FieldPolicyHead,
    FieldPolicyObjectState,
)
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions as transactions,
)
from app.modules.system.service.generic_field_policy_admission import (
    require_user_policy_compatibility,
)
from app.modules.system.service.generic_field_policy_migration import migrate_legacy
from app.modules.system.service.generic_field_policy_service import (
    generic_field_policy_service as policies,
)
from app.modules.system.service.generic_tenant_policy import prepare_new_tenant_policy
from tests.modules.platform.test_authorization_bootstrap_candidate import bootstrap
from tests.modules.platform.test_authorization_bootstrap_candidate import (
    prepare as prepare_bootstrap,
)
from tests.modules.system.test_authorization_tenant_candidate import (  # noqa: F401
    plan_sessions,
    run,
)
from tests.modules.system.test_authorization_tenant_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_generic_policy_migration import fence


async def prepare_generic(system_sessions):
    async with system_sessions[1]() as db:
        await migrate_legacy(
            db,
            fence=await fence(db),
            catalog=production_catalog(),
            catalog_revision="test/new-tenant",
            source_build="b" * 40,
        )
        await db.commit()


async def test_new_tenant_reserves_empty_generic_state_in_the_same_prepare_transaction(
    system_sessions,
):
    factory, plain = system_sessions
    await prepare_generic(system_sessions)
    payload = {
        "tenant_code": "new-customer",
        "tenant_name": "New Customer",
        "idempotency_key": "generic-tenant-prepare-001",
    }
    await run(factory, "tenant.prepare", 50, payload)
    async with plain() as db:
        head = await db.get(FieldPolicyHead, 50)
        assert head is not None, "new generic tenant cannot fall back to legacy fields"
        assert (
            head.rollout_state == "preparing"
            and head.active_manifest_id is None
            and not head.ever_enforced
        )
        objects = (
            await db.scalars(
                select(FieldPolicyObjectState).where(
                    FieldPolicyObjectState.tenant_id == 50
                )
            )
        ).all()
        assert {row.object_id for row in objects} == {
            obj.object_id
            for obj in production_catalog().objects.values()
            if obj.domain_kind == "tenant"
        }
        assert all(row.mode == "legacy" and not row.ever_enforced for row in objects)
    await run(factory, "tenant.prepare", 50, payload)
    async with plain() as db:
        assert (await db.get(FieldPolicyHead, 50)).head_version == 1


async def test_missing_tenant_head_cannot_reopen_old_policy_writes_in_generic_deployment(
    system_sessions,
):
    await prepare_generic(system_sessions)
    async with system_sessions[1]() as db:
        await db.execute(
            insert(Tenant).values(
                tenant_id=72,
                tenant_code="missing-head",
                tenant_name="Missing policy",
                lifecycle_state="prepared",
                status="2",
                bootstrap_version=0,
                row_version=1,
            )
        )
        await db.commit()
        with pytest.raises(BusinessException) as denied:
            await require_user_policy_compatibility(db, tenant_id=72)
        assert denied.value.error_code == "FIELD_POLICY_CLIENT_UPGRADE_REQUIRED"


async def test_new_tenant_bootstrap_can_manage_policy_while_business_is_closed(
    system_sessions, monkeypatch
):
    factory, plain = system_sessions
    await prepare_generic(system_sessions)
    await prepare_bootstrap(factory, plain)
    await bootstrap(factory)
    monkeypatch.setattr(settings, "TENANT_MODE", "hosted")
    monkeypatch.setattr(settings, "TENANT_HOSTED_LOGIN_ENABLED", True)
    await run(factory, "tenant.activate", 50)
    async with plain() as db:
        actor = await db.scalar(
            select(User).where(User.tenant_id == 50, User.user_name == "admin")
        )
        tenant = await db.get(Tenant, 50)
        identity = TenantContext(
            50, "bootstrap-new", actor.user_id, tenant.row_version, "access_token"
        )
        version = actor.auth_version
    async with factory() as db:
        catalog = await FieldManagementOwner().execute(
            db,
            tenant=identity,
            actor_user_version=version,
            catalog=production_catalog(),
            operation="catalog",
            payload={},
        )
    assert catalog["tenantId"] == "50" and catalog["capabilities"]["edit"]
    async with factory() as db:
        try:
            async with transactions.scope(db, tenants=(TenantLock(50),)) as tx:
                with pytest.raises(BusinessException) as denied:
                    await policies.load_active(
                        db,
                        tx,
                        tenant_id=50,
                        catalog=production_catalog(),
                        object_id="system.user",
                        role_ids=(),
                    )
                assert denied.value.error_code == "FIELD_POLICY_UNAVAILABLE"
        finally:
            await db.rollback()


@pytest.mark.parametrize("fault", ["missing_object", "repeated"])
async def test_creation_policy_requires_complete_unused_reservations(
    system_sessions, fault
):
    factory, plain = system_sessions
    await prepare_generic(system_sessions)
    key = ResourceKey(ResourceKind.TENANT, None, 51)
    async with factory() as db:
        try:
            with pytest.raises(BusinessException) as rejected:
                async with transactions.scope_creation(db) as tx:
                    await tx.create_tenant(
                        key, {"tenant_code": "proof-new", "tenant_name": "Proof"}
                    )
                    policy = await prepare_new_tenant_policy(db, tx, 51)
                    assert policy is not None
                    await tx.lock(
                        LockPlan(
                            existing=(key,),
                            new=policy.new[:-1]
                            if fault == "missing_object"
                            else policy.new,
                        )
                    )
                    await tx.initialize_tenant_policy(
                        51, catalog_digest=policy.catalog_digest, objects=policy.objects
                    )
                    await tx.initialize_tenant_policy(
                        51, catalog_digest=policy.catalog_digest, objects=policy.objects
                    )
            assert rejected.value.error_code == "FIELD_POLICY_LOCK_ORDER"
        finally:
            await db.rollback()
    async with plain() as db:
        assert await db.get(Tenant, 51) is None
        assert await db.get(FieldPolicyHead, 51) is None
