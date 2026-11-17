"""Real current-identity authorization, tenant receipts and transaction rollback."""

import pytest
from sqlalchemy import select, update

from app.core.authorization_plan import ResourceKind
from app.core.exceptions import BusinessException
from app.db.authorization_generic_policy_owner import GenericFieldPolicyOwner
from app.modules.system.models.field_policy import (
    FieldPolicyAudit,
    FieldPolicyHead,
    FieldPolicyRevision,
)
from app.modules.system.models.role import Role
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from app.modules.system.service.authorization_transaction_service import _Transaction
from tests.modules.system.test_generic_policy_persistence import (
    generic_sessions as generic_sessions,
)
from tests.modules.system.test_generic_policy_persistence import policies as policies
from tests.modules.system.test_generic_policy_persistence import publish
from tests.modules.system.test_planned_user_field_policy import TENANT
from tests.modules.system.test_planned_user_field_policy import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_planned_user_field_policy import (
    policy_sessions as policy_sessions,
)
from tests.modules.system.test_user_field_policy_admin_owner import (
    admin_sessions as admin_sessions,
)
from tests.modules.system.test_user_field_policy_admin_owner import call as old_call
from tests.modules.system.test_user_field_policy_admin_owner import grant


@pytest.fixture
async def managed(policies, admin_sessions):
    assert policies[:2] == admin_sessions
    return policies


async def call(managed, command, *, version=1):
    factory, _, catalog = managed
    async with factory() as db:
        return await GenericFieldPolicyOwner().execute(
            db,
            tenant=TENANT,
            actor_user_version=version,
            catalog=catalog,
            command=command,
        )


async def test_current_super_role_requires_dedicated_permission(managed):
    _, plain, _ = managed
    with pytest.raises(BusinessException) as error:
        await call(managed, {"action": "read"})
    assert error.value.error_code == "FIELD_POLICY_MANAGEMENT_FORBIDDEN"
    await grant(plain, "system:role:field-policy-view")
    assert (await call(managed, {"action": "read"}))["schema_version"] == 2
    with pytest.raises(BusinessException):
        await call(managed, publish())
    async with plain() as db:
        await db.execute(update(Role).where(Role.role_id == 201).values(status="2"))
        await db.commit()
    with pytest.raises(BusinessException):
        await call(managed, {"action": "read"})


async def test_owner_commit_receipt_and_stale_identity(managed):
    _, plain, _ = managed
    await grant(plain, "system:role:field-policy-edit")
    command = publish()
    with pytest.raises(BusinessException):
        await call(managed, command, version=999)
    result = await call(managed, command)
    assert await call(managed, command) == result
    await grant(plain, "system:role:field-policy-view")
    assert (
        await call(managed, {"action": "receipt", "request_id": command["request_id"]})
        == result
    )
    async with plain() as db:
        assert len((await db.scalars(select(FieldPolicyAudit))).all()) == 1
        assert (
            await db.scalar(
                select(UserFieldPolicyState.epoch).where(
                    UserFieldPolicyState.tenant_id == 1
                )
            )
            == 1
        )


async def test_audit_failure_rolls_back_revision_head_and_epoch(managed, monkeypatch):
    _, plain, _ = managed
    await grant(plain, "system:role:field-policy-edit")
    original = _Transaction.insert_reserved

    async def fail(self, key, values):
        if key.kind is ResourceKind.FIELD_POLICY_AUDIT:
            raise RuntimeError("injected audit storage failure")
        return await original(self, key, values)

    monkeypatch.setattr(_Transaction, "insert_reserved", fail)
    with pytest.raises(RuntimeError, match="injected audit"):
        await call(managed, publish())
    async with plain() as db:
        assert not (await db.scalars(select(FieldPolicyRevision))).all()
        assert (
            await db.scalar(
                select(FieldPolicyHead.head_version).where(
                    FieldPolicyHead.tenant_id == 0
                )
            )
            == 1
        )
        assert (
            await db.scalar(
                select(UserFieldPolicyState.epoch).where(
                    UserFieldPolicyState.tenant_id == 0
                )
            )
            == 1
        )


async def test_old_owner_rejected_server_side_after_generic_migration(managed):
    factory, plain, _ = managed
    await grant(plain, "system:user:field-policy-view")
    with pytest.raises(BusinessException) as error:
        await old_call(factory, "read")
    assert error.value.error_code == "FIELD_POLICY_CLIENT_UPGRADE_REQUIRED"
