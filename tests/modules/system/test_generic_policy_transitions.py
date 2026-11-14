"""Watermarks, candidate isolation, authorization invalidation and concurrent CAS."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from app.core.authorization_plan import LockMode, LockPlan, ResourceKey, TenantLock
from app.core.authorization_plan import ResourceKind as K
from app.core.config import settings
from app.core.exceptions import BusinessException
from app.core.field_policy.documents import catalog_parts
from app.core.field_policy.qualification import Qualification
from app.modules.system.models.field_policy import FieldPolicyAudit, FieldPolicyHead
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions as transactions,
)
from app.modules.system.service.generic_field_policy_service import (
    generic_field_policy_service as service,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions,  # noqa: F401
)
from tests.modules.system.test_generic_policy_persistence import (  # noqa: F401
    policies as _policies,
)
from tests.modules.system.test_generic_policy_persistence import (
    publish,
    run,
)
from tests.modules.system.test_generic_policy_schema import (
    generic_sessions,  # noqa: F401
)

policies = _policies


def command(action, version, **extra):
    return {
        "action": action,
        "expected_version": version,
        "request_id": str(uuid4()),
        "reason": "Transition regression",
        **extra,
    }


async def ready(policies, monkeypatch):
    monkeypatch.setattr(settings, "RELEASE_BUILD_SHA", "b" * 40)
    catalog = policies[2]
    # Synthetic test evidence exercises storage only, not an actual release qualification.
    proof = Qualification(
        "b" * 40,
        catalog.digest,
        *catalog_parts(catalog),
        "c" * 64,
        datetime.now(UTC) + timedelta(minutes=30),
    )
    await run(policies, publish())
    built = await run(policies, command("build", 2))
    marked = await run(
        policies,
        command("ready", 3, expected_epoch=built["epoch"]),
        qualification=proof,
    )
    return proof, marked


async def test_ready_stores_its_own_post_write_epoch_and_activation_is_atomic(
    policies, monkeypatch
):
    proof, marked = await ready(policies, monkeypatch)
    _, plain, _ = policies
    async with plain() as db:
        head = await db.get(FieldPolicyHead, 0)
        assert head.ready_b_epoch == marked["epoch"]
        assert head.active_manifest_id is None
    activated = await run(
        policies,
        command("activate", 4, expected_epoch=marked["epoch"]),
        qualification=proof,
    )
    assert activated["activeManifestDigest"] == marked["candidateManifestDigest"]
    factory, _, catalog = policies
    async with factory() as db:
        async with transactions.scope(db, tenants=(TenantLock(0),)) as tx:
            assert (
                await service.load_active(
                    db,
                    tx,
                    tenant_id=0,
                    catalog=catalog,
                    object_id="sample.item",
                    role_ids=("201",),
                )
                == ()
            )
        await db.rollback()
    await run(policies, publish(version=5) | {"binding_version": 1})
    async with plain() as db:
        head = await db.get(FieldPolicyHead, 0)
        assert head.active_manifest_digest == activated["activeManifestDigest"]
        assert head.candidate_manifest_digest is None


async def test_role_change_invalidates_ready_without_erasing_candidate(
    policies, monkeypatch
):
    proof, marked = await ready(policies, monkeypatch)
    factory, plain, _ = policies
    async with factory() as db:
        async with transactions.scope(
            db, tenants=(TenantLock(0, LockMode.EXCLUSIVE),)
        ) as tx:
            role = ResourceKey(K.ROLE, 0, 201)
            await tx.lock(LockPlan(existing=(role,)))
            async with tx.aggregate(0):
                await tx.update_held(role, {"data_scope": "5"})
        await db.commit()
    async with plain() as db:
        head = await db.get(FieldPolicyHead, 0)
        assert (
            head.ready_b_epoch is None
            and head.candidate_manifest_digest == marked["candidateManifestDigest"]
        )
    with pytest.raises(BusinessException):
        await run(
            policies,
            command("activate", 4, expected_epoch=marked["epoch"]),
            qualification=proof,
        )


async def test_same_version_concurrent_publish_has_one_winner(policies):
    results = await asyncio.gather(
        run(policies, publish()), run(policies, publish()), return_exceptions=True
    )
    assert sum(type(r) is dict for r in results) == 1
    assert sum(isinstance(r, BusinessException) for r in results) == 1
    _, plain, _ = policies
    async with plain() as db:
        assert len((await db.scalars(select(FieldPolicyAudit))).all()) == 1


async def test_missing_qualification_cannot_mark_ready(policies):
    await run(policies, publish())
    built = await run(policies, command("build", 2))
    with pytest.raises(BusinessException) as exc:
        await run(policies, command("ready", 3, expected_epoch=built["epoch"]))
    assert exc.value.error_code == "FIELD_POLICY_ACTIVATION_BLOCKED"
