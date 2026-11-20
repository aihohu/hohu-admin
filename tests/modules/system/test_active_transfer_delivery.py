"""Real artifact owners consume active manifests and invalidate legacy evidence."""

from io import BytesIO

import pytest
from openpyxl import load_workbook
from sqlalchemy import select, update

from app.core.authorization_plan import LockMode, TenantLock
from app.core.config import settings
from app.core.exceptions import BusinessException
from app.core.file_storage import LocalFileStorage
from app.db.authorization_transfer_owner import TransferOwner
from app.modules.system.field_policy.evaluator import FieldGrant
from app.modules.system.models.user_field_policy import UserFieldPolicyState as State
from app.modules.system.models.user_transfer import UserImportBatch
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.planned_user_field_policy_service import (
    planned_user_field_policy_service as policies,
)
from app.modules.system.service.user_field_policy_admin import policy_catalog
from tests.modules.system.test_authorization_import_candidate import (
    import_sessions as import_sessions,
)


@pytest.fixture
async def active_import_sessions(import_sessions):
    factory, plain, tenant = import_sessions
    # The older transfer fixture uses an intentionally synthetic ready digest.
    # Active tests must start with a real empty manifest before publication.
    async with plain() as db:
        await db.execute(
            update(State)
            .where(State.tenant_id == tenant.tenant_id)
            .values(
                mode="legacy", candidate_manifest_digest=None, epoch=State.epoch + 1
            )
        )
        await db.commit()
    return factory, plain, tenant


async def policy_change(factory, tenant, action, **kwargs):
    async with factory() as db:
        async with authorization_transactions.scope(
            db, tenants=(TenantLock(tenant.tenant_id, LockMode.EXCLUSIVE),)
        ) as tx:
            plan = await policies.plan(
                db,
                tx,
                tenant=tenant,
                action=action,
                role_id=201 if action == "publish" else None,
            )
            await tx.lock(plan.lock_plan)
            method = (
                policies.publish
                if action == "publish"
                else policies.mark_ready
                if action == "ready"
                else policies.transition
            )
            result = await method(
                db,
                tx,
                plan,
                tenant=tenant,
                reason="active delivery test",
                correlation_id="active-delivery",
                **kwargs,
            )
        await db.commit()
        return result


async def enable(factory, tenant, rules):
    await policy_change(factory, tenant, "publish", grants=rules, expected_version=0)
    async with factory() as db:
        state = await policies._state(db, tenant)
        args = {
            "expected_epoch": state["epoch"],
            "expected_manifest_digest": state["candidate_manifest_digest"],
        }
        await db.rollback()
    ready = await policy_change(factory, tenant, "ready", **args)
    await policy_change(
        factory,
        tenant,
        "activate",
        expected_epoch=ready.epoch,
        expected_manifest_digest=ready.candidate_manifest_digest,
    )


async def test_active_export_and_old_download_revocation(
    active_import_sessions, tmp_path, monkeypatch
):
    factory, _, tenant = active_import_sessions
    owner = TransferOwner(factory, LocalFileStorage(tmp_path))
    legacy = await owner.export(
        tenant=tenant, actor_user_version=1, args={"reason": "legacy"}
    )
    rules = tuple(
        FieldGrant("201", field["fieldId"], option["operation"], option["purpose"])
        for field in policy_catalog()["fields"]
        for option in field["options"]
        if option["purpose"] in {"management.read", "management.query", "export"}
    )
    monkeypatch.setattr(settings, "USER_FIELD_POLICY_AVAILABLE", True)
    await enable(factory, tenant, rules)
    with pytest.raises(BusinessException):
        await owner.download(
            tenant=tenant,
            actor_user_version=1,
            kind="export",
            identifier=legacy.resource_id,
            administrative=True,
        )
    exported = await owner.export(
        tenant=tenant, actor_user_version=1, args={"reason": "active"}
    )
    workbook = load_workbook(BytesIO(exported.data), read_only=True)
    assert workbook.active.max_row > 1
    workbook.close()
    await policy_change(factory, tenant, "publish", grants=(), expected_version=1)
    with pytest.raises(BusinessException):
        await owner.download(
            tenant=tenant,
            actor_user_version=1,
            kind="export",
            identifier=exported.resource_id,
            administrative=True,
        )


async def test_active_import_deny_has_no_preview_or_artifact(
    active_import_sessions, tmp_path, monkeypatch
):
    factory, plain, tenant = active_import_sessions
    monkeypatch.setattr(settings, "USER_FIELD_POLICY_AVAILABLE", True)
    await enable(factory, tenant, ())
    with pytest.raises(BusinessException):
        await TransferOwner(factory, LocalFileStorage(tmp_path)).preview(
            tenant=tenant,
            actor_user_version=1,
            file_bytes=b"user_name,dept_input\nnewperson,Team\n",
            filename="input.csv",
            reason="test",
        )
    async with plain() as db:
        assert await db.scalar(select(UserImportBatch.batch_id)) is None
    assert not list(tmp_path.rglob("payload"))
