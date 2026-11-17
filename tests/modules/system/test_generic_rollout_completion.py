"""A completion receipt proves the whole tenant set and sealed active snapshots."""

from uuid import uuid4

import pytest
from sqlalchemy import delete, update

from app.core.authorization_plan import TenantLock
from app.core.exceptions import BusinessException
from app.core.field_policy.qualification import read_server_qualification
from app.db import authorization_rollout_completion_owner as completion_owner
from app.db.authorization_rollout_completion_owner import RolloutCompletionOwner
from app.db.base import role_menus
from app.modules.system.models.field_policy import FieldPolicyObjectState
from app.modules.system.models.tenant import Tenant
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions as transactions,
)
from app.modules.system.service.policy_rollout_completion import capture_completion
from tests.modules.system.test_generic_activation_management import (  # noqa: F401
    admin_sessions,
    generic_sessions,
    plan_sessions,
    policies,
    policy_sessions,
    setup_candidate,
    target,
    transition,
)
from tests.modules.system.test_generic_activation_management import managed as managed
from tests.modules.system.test_generic_policy_persistence import run
from tests.modules.system.test_planned_user_field_policy import TENANT
from tools.ops.policy_preparation_identity import require_preparation_administrators


async def capture(managed, proof):
    async with managed[0]() as db:
        try:
            async with transactions.scope_global(db) as tx:
                return await capture_completion(
                    db, tx, catalog=managed[2], qualification=proof
                )
        finally:
            await db.rollback()


async def test_completion_requires_every_tenant_and_verifies_active_object_digest(
    managed, tmp_path, monkeypatch
):
    await setup_candidate(managed, tmp_path, monkeypatch)
    proof = read_server_qualification(catalog=managed[2], required=True)
    for action in ("ready", "activate"):
        manifest_id, payload = await target(managed)
        await transition(managed, action, manifest_id, payload)
    with pytest.raises(BusinessException):
        await capture(managed, proof)
    for action, version in (("build", 1), ("ready", 2), ("activate", 3)):
        cmd = {
            "action": action,
            "request_id": str(uuid4()),
            "expected_version": version,
            "reason": "tenant one fixture",
        }
        if action in {"ready", "activate"}:
            cmd["expected_epoch"] = version
        await run(managed, cmd, tenant_id=1, qualification=proof)
    completed = await capture(managed, proof)
    assert completed["tenant_count"] == 2
    assert completed["qualification_digest"] == proof.evidence_digest
    assert len(completed["state_digest"]) == 64
    async with managed[0]() as db:
        owned = await RolloutCompletionOwner().execute(
            db, tenant=TENANT, actor_user_version=1, catalog=managed[2]
        )
        assert owned["state_digest"] == completed["state_digest"]
        assert not db.in_transaction()
    original = completion_owner.rollback_authorization_session
    attempts = []

    async def uncertain(db):
        attempts.append(1)
        await original(db)
        raise BusinessException(
            503, "unknown rollback", error_code="AUTHORIZATION_ROLLBACK_UNKNOWN"
        )

    with monkeypatch.context() as patch:
        patch.setattr(
            "app.db.authorization_rollout_completion_owner.rollback_authorization_session",
            uncertain,
        )
        async with managed[0]() as db:
            with pytest.raises(BusinessException) as unknown:
                await RolloutCompletionOwner().execute(
                    db, tenant=TENANT, actor_user_version=1, catalog=managed[2]
                )
            assert unknown.value.error_code == "AUTHORIZATION_ROLLBACK_UNKNOWN"
    assert attempts == [1]
    async with managed[1]() as db:
        await db.execute(
            delete(role_menus).where(
                role_menus.c.tenant_id == 0, role_menus.c.menu_id == 303
            )
        )
        await db.commit()
    async with managed[0]() as db:
        with pytest.raises(BusinessException) as denied:
            await RolloutCompletionOwner().execute(
                db, tenant=TENANT, actor_user_version=1, catalog=managed[2]
            )
        assert denied.value.error_code == "FIELD_POLICY_MANAGEMENT_FORBIDDEN"
    async with managed[1]() as db:
        await db.execute(
            update(FieldPolicyObjectState)
            .where(FieldPolicyObjectState.tenant_id == 1)
            .values(active_object_digest="c" * 64)
        )
        await db.commit()
    with pytest.raises(BusinessException):
        await capture(managed, proof)


async def test_completion_cannot_use_one_tenant_shared_barrier(
    managed, tmp_path, monkeypatch
):
    await setup_candidate(managed, tmp_path, monkeypatch)
    proof = read_server_qualification(catalog=managed[2], required=True)
    async with managed[0]() as db:
        try:
            async with transactions.scope(db, tenants=(TenantLock(0),)) as tx:
                with pytest.raises(BusinessException) as denied:
                    await capture_completion(
                        db, tx, catalog=managed[2], qualification=proof
                    )
                assert denied.value.error_code == "FIELD_POLICY_LOCK_ORDER"
        finally:
            await db.rollback()


async def test_disabled_preparing_tenant_is_quarantined_without_implicit_grants(
    managed, tmp_path, monkeypatch
):
    await setup_candidate(managed, tmp_path, monkeypatch)
    proof = read_server_qualification(catalog=managed[2], required=True)
    for action in ("ready", "activate"):
        manifest_id, payload = await target(managed)
        await transition(managed, action, manifest_id, payload)
    async with managed[1]() as db:
        await db.execute(
            update(Tenant)
            .where(Tenant.tenant_id == 1)
            .values(status="2", lifecycle_state="disabled", row_version=2)
        )
        await db.commit()
    completed = await capture(managed, proof)
    assert completed["tenant_count"] == 2 and completed["quarantined_count"] == 1
    async with managed[1]() as db:
        await db.execute(
            update(Tenant)
            .where(Tenant.tenant_id == 1)
            .values(status="1", lifecycle_state="active", row_version=3)
        )
        await db.commit()
    with pytest.raises(BusinessException):
        await capture(managed, proof)


async def test_upgrade_preflight_cannot_skip_initialized_disabled_tenant(
    managed, tmp_path, monkeypatch
):
    await setup_candidate(managed, tmp_path, monkeypatch)
    async with managed[1]() as db:
        await db.execute(
            update(Tenant)
            .where(Tenant.tenant_id == 1)
            .values(status="2", lifecycle_state="disabled", row_version=2)
        )
        await db.commit()
    with pytest.raises(BusinessException) as rejected:
        await require_preparation_administrators(managed[1])
    assert rejected.value.error_code == "FIELD_POLICY_ADMIN_PREFLIGHT_REQUIRED"
