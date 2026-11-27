"""Repeat stopped preparation validates and preserves already active policy facts."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from app.core.config import settings
from app.core.exceptions import BusinessException
from app.core.field_policy import Catalog
from app.core.field_policy.documents import catalog_parts
from app.core.field_policy.qualification import Qualification
from app.modules.system.field_policy.generic_compat import user_compat_object
from app.modules.system.field_policy.ordinary_catalog import production_catalog
from app.modules.system.models.field_policy import (
    FieldPolicyAudit,
    FieldPolicyBinding,
    FieldPolicyHead,
    FieldPolicyManifest,
    FieldPolicyManifestItem,
    FieldPolicyObjectState,
    FieldPolicyRequest,
    FieldPolicyRevision,
)
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyGlobalGuard,
    UserFieldPolicyState,
)
from app.modules.system.service.generic_field_policy_migration import migrate_legacy
from app.modules.system.service.ordinary_catalog_upgrade import prepare_catalog
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_generic_policy_migration import fence
from tests.modules.system.test_generic_policy_persistence import run
from tests.modules.system.test_generic_policy_transitions import command


async def active(system_sessions, monkeypatch):
    factory, plain = system_sessions
    monkeypatch.setattr(settings, "RELEASE_BUILD_SHA", "b" * 40)
    catalog = production_catalog()
    async with plain() as db:
        await prepare_catalog(db, fence=await fence(db), source_build="b" * 40)
        await db.commit()
    policies = factory, plain, catalog
    proof = Qualification(
        "b" * 40,
        catalog.digest,
        *catalog_parts(catalog),
        "c" * 64,
        datetime.now(UTC) + timedelta(minutes=30),
    )
    built = await run(policies, command("build", 1))
    ready = await run(
        policies,
        command("ready", 2, expected_epoch=built["epoch"]),
        qualification=proof,
    )
    result = await run(
        policies,
        command("activate", 3, expected_epoch=ready["epoch"]),
        qualification=proof,
    )
    return policies, result, proof


async def snapshot(plain):
    async with plain() as db:
        return {
            model.__tablename__: sorted(
                (
                    tuple(row)
                    for row in (await db.execute(select(model.__table__))).all()
                ),
                key=str,
            )
            for model in (
                UserFieldPolicyGlobalGuard,
                UserFieldPolicyState,
                FieldPolicyHead,
                FieldPolicyObjectState,
                FieldPolicyBinding,
                FieldPolicyRevision,
                FieldPolicyManifest,
                FieldPolicyManifestItem,
                FieldPolicyRequest,
                FieldPolicyAudit,
            )
        }


@pytest.mark.parametrize("frozen", [False, True])
@pytest.mark.parametrize("new_candidate", [False, True])
async def test_current_catalog_reprepare_preserves_active_and_frozen_facts(
    system_sessions, monkeypatch, frozen, new_candidate
):
    policies, result, _ = await active(system_sessions, monkeypatch)
    if new_candidate:
        result = await run(policies, command("build", int(result["headVersion"])))
    if frozen:
        await run(
            policies,
            command(
                "freeze",
                int(result["headVersion"]),
                expected_epoch=result["epoch"],
                object_id="system.user",
            ),
        )
    before = await snapshot(policies[1])
    for _ in range(2):
        async with policies[1]() as db:
            response = await prepare_catalog(
                db, fence=await fence(db), source_build="b" * 40
            )
            await db.commit()
        assert response["changedTenants"] == [] and response["revisions"] == []
        assert await snapshot(policies[1]) == before


@pytest.mark.parametrize("fault", ["head", "object", "barrier", "ready"])
async def test_current_catalog_reprepare_refuses_corrupt_active_facts(
    system_sessions, monkeypatch, fault
):
    policies, _, _ = await active(system_sessions, monkeypatch)
    initial = await snapshot(policies[1])
    async with policies[1]() as db:
        if fault == "head":
            statement = (
                update(FieldPolicyHead)
                .where(FieldPolicyHead.tenant_id == 0)
                .values(active_manifest_digest="d" * 64)
            )
        elif fault == "object":
            statement = (
                update(FieldPolicyObjectState)
                .where(
                    FieldPolicyObjectState.tenant_id == 0,
                    FieldPolicyObjectState.object_id == "system.role",
                )
                .values(active_object_digest="d" * 64)
            )
        elif fault == "barrier":
            statement = (
                update(UserFieldPolicyState)
                .where(UserFieldPolicyState.tenant_id == 0)
                .values(
                    active_manifest_digest="d" * 64,
                    candidate_manifest_digest="d" * 64,
                    epoch=UserFieldPolicyState.epoch + 1,
                )
            )
        else:
            statement = (
                update(FieldPolicyHead)
                .where(FieldPolicyHead.tenant_id == 0)
                .values(ready_manifest_digest="d" * 64)
            )
        if fault in {"head", "ready"}:
            # These malformed pointers are already rejected by the real DDL.
            with pytest.raises(IntegrityError):
                await db.execute(statement)
            await db.rollback()
        else:
            await db.execute(statement)
            await db.commit()
    if fault in {"head", "ready"}:
        assert await snapshot(policies[1]) == initial
        return
    before = await snapshot(policies[1])
    async with policies[1]() as db:
        with pytest.raises(BusinessException):
            await prepare_catalog(db, fence=await fence(db), source_build="b" * 40)
        await db.rollback()
    assert await snapshot(policies[1]) == before


async def test_reprepare_can_repair_expired_ready_without_consuming_it(
    system_sessions, monkeypatch
):
    policies, result, proof = await active(system_sessions, monkeypatch)
    built = await run(policies, command("build", int(result["headVersion"])))
    await run(
        policies,
        command("ready", int(built["headVersion"]), expected_epoch=built["epoch"]),
        qualification=proof,
    )
    async with policies[1]() as db:
        await db.execute(
            update(FieldPolicyHead)
            .where(FieldPolicyHead.tenant_id == 0)
            .values(ready_until=datetime.now(UTC) - timedelta(seconds=10))
        )
        await db.commit()
    before = await snapshot(policies[1])
    async with policies[1]() as db:
        await prepare_catalog(db, fence=await fence(db), source_build="b" * 40)
        await db.commit()
    assert await snapshot(policies[1]) == before


async def test_reprepare_never_expands_an_already_enforced_historical_catalog(
    system_sessions, monkeypatch
):
    factory, plain = system_sessions
    monkeypatch.setattr(settings, "RELEASE_BUILD_SHA", "b" * 40)
    old = Catalog((user_compat_object(),), ())
    async with plain() as db:
        await migrate_legacy(
            db,
            fence=await fence(db),
            catalog=old,
            catalog_revision="system-fields/2-prepare",
            source_build="b" * 40,
        )
        await db.commit()
    policies = factory, plain, old
    proof = Qualification(
        "b" * 40,
        old.digest,
        *catalog_parts(old),
        "c" * 64,
        datetime.now(UTC) + timedelta(minutes=30),
    )
    built = await run(policies, command("build", 1))
    marked = await run(
        policies,
        command("ready", 2, expected_epoch=built["epoch"]),
        qualification=proof,
    )
    await run(
        policies,
        command("activate", 3, expected_epoch=marked["epoch"]),
        qualification=proof,
    )
    before = await snapshot(plain)
    async with plain() as db:
        with pytest.raises(BusinessException):
            await prepare_catalog(db, fence=await fence(db), source_build="b" * 40)
        await db.rollback()
    assert await snapshot(plain) == before
