"""Candidate/active separation and atomic receipts under the existing G/B."""

from uuid import uuid4

import pytest
from sqlalchemy import insert, select

from app.core.authorization_plan import LockMode, TenantLock
from app.core.exceptions import BusinessException
from app.core.field_policy import Access, Catalog, FieldSpec, ObjectSpec
from app.core.field_policy.contracts import primitive
from app.core.field_policy.documents import catalog_document, catalog_parts, digest
from app.core.tenant_scope import TenantContext
from app.modules.system.models.field_policy import (
    FieldCatalogObject,
    FieldCatalogSnapshot,
    FieldPolicyAudit,
    FieldPolicyHead,
    FieldPolicyObjectState,
    FieldPolicyRequest,
)
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions as transactions,
)
from app.modules.system.service.generic_field_policy_service import (
    generic_field_policy_service as service,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions,  # noqa: F401
)
from tests.modules.system.test_generic_policy_schema import (
    generic_sessions as _generic_sessions,
)

generic_sessions = _generic_sessions


@pytest.fixture
async def policies(generic_sessions):
    factory, plain = generic_sessions
    catalog = Catalog(
        (
            ObjectSpec(
                "sample.item",
                "tenant",
                "sample.scope",
                (FieldSpec("name", "string", (Access("read", "management.read"),)),),
            ),
        ),
        (),
    )
    action, adapter = catalog_parts(catalog)
    async with plain() as db:
        await db.execute(
            insert(FieldCatalogSnapshot).values(
                catalog_digest=catalog.digest,
                schema_version=2,
                catalog_revision="test/1",
                action_catalog_digest=action,
                adapter_digest=adapter,
                canonical_document=catalog_document(catalog),
            )
        )
        await db.execute(
            insert(FieldCatalogObject).values(
                catalog_digest=catalog.digest,
                object_id="sample.item",
                domain_kind="tenant",
                schema_digest=digest(primitive(catalog.objects["sample.item"])),
                adapter_version="sample.scope",
            )
        )
        for tid in (0, 1):
            await db.execute(
                insert(FieldPolicyHead).values(
                    tenant_id=tid, catalog_digest=catalog.digest
                )
            )
            await db.execute(
                insert(FieldPolicyObjectState).values(
                    tenant_id=tid,
                    object_id="sample.item",
                    catalog_digest=catalog.digest,
                )
            )
        await db.commit()
    return factory, plain, catalog


async def run(policies, command, *, tenant_id=0, qualification=None):
    factory, _, catalog = policies
    tenant = TenantContext(
        tenant_id, f"plan-{tenant_id}", 101 + tenant_id, 1, "access_token"
    )
    async with factory() as db:
        try:
            async with transactions.scope(
                db, tenants=(TenantLock(tenant_id, LockMode.EXCLUSIVE),)
            ) as tx:
                plan = await service.plan(
                    db,
                    tx,
                    tenant=tenant,
                    catalog=catalog,
                    command=command,
                    qualification=qualification,
                )
                await tx.lock(plan.lock_plan)
                result = await service.apply(
                    db, tx, plan, tenant=tenant, catalog=catalog
                )
            await db.commit()
            return result
        except BaseException:
            await db.rollback()
            raise


def publish(*, version=1, request_id=None, role_id=201):
    return {
        "action": "publish",
        "request_id": request_id or str(uuid4()),
        "expected_version": version,
        "reason": "Explicit empty candidate",
        "role_id": role_id,
        "object_id": "sample.item",
        "binding_version": 0,
        "rules": [],
    }


async def test_publish_replay_is_one_epoch_one_audit_and_no_active(policies):
    cmd = publish()
    first = await run(policies, cmd)
    assert first["revisionId"].isdigit()
    assert await run(policies, cmd) == first
    _, plain, _ = policies
    async with plain() as db:
        receipt = (await db.scalars(select(FieldPolicyRequest))).one()
        assert str(receipt.revision_id) == first["revisionId"]
        assert (receipt.tenant_id, receipt.role_id, receipt.object_id) == (
            0,
            201,
            "sample.item",
        )
        assert len((await db.scalars(select(FieldPolicyAudit))).all()) == 1
        assert len((await db.scalars(select(FieldPolicyRequest))).all()) == 1
        assert (await db.get(UserFieldPolicyState, 0)).epoch == 2
        head = await db.get(FieldPolicyHead, 0)
        assert head.head_version == 2 and head.active_manifest_id is None
        assert (await db.get(FieldPolicyHead, 1)).head_version == 1


async def test_same_request_with_changed_command_conflicts(policies):
    cmd = publish()
    await run(policies, cmd)
    with pytest.raises(BusinessException) as exc:
        await run(policies, cmd | {"reason": "different command"})
    assert exc.value.error_code == "FIELD_POLICY_REQUEST_CONFLICT"


async def test_stale_cas_and_cross_tenant_role_do_not_write(policies):
    await run(policies, publish())
    with pytest.raises(BusinessException):
        await run(policies, publish())
    with pytest.raises(BusinessException):
        await run(policies, publish(role_id=202, version=2))
    _, plain, _ = policies
    async with plain() as db:
        assert (await db.get(FieldPolicyHead, 0)).head_version == 2


async def test_build_seals_candidate_without_activating(policies):
    await run(policies, publish())
    result = await run(
        policies,
        {
            "action": "build",
            "request_id": str(uuid4()),
            "expected_version": 2,
            "reason": "Build complete candidate",
        },
    )
    assert result["candidateManifestDigest"]
    _, plain, _ = policies
    async with plain() as db:
        head = await db.get(FieldPolicyHead, 0)
        assert head.active_manifest_id is None
        assert head.candidate_manifest_digest == result["candidateManifestDigest"]
