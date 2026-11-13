"""The current catalog can advance without rewriting its historical source."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select, update

from app.core.config import settings
from app.core.exceptions import BusinessException
from app.core.field_policy import Access, Catalog, FieldRef, FieldSpec, Grant
from app.core.field_policy.documents import catalog_parts, rules_document
from app.core.field_policy.qualification import Qualification
from app.modules.system.field_policy import ordinary_catalog as catalogs
from app.modules.system.field_policy.generic_compat import user_compat_object
from app.modules.system.models import field_policy as models
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from app.modules.system.service.generic_field_policy_migration import (
    migrate_legacy,
    register_catalog,
)
from app.modules.system.service.generic_field_policy_service import (
    generic_field_policy_service as policies_service,
)
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


def test_four_object_history_keeps_its_original_identity():
    history = getattr(catalogs, "ordinary_catalog", None)
    assert callable(history), (
        "The four-object upgrade source needs an immutable builder"
    )
    catalog = history()
    assert (
        catalog.digest
        == "1bba4c34c8f8e6edd636ed8b2447f8fbd0053e7082670846aa11ab25db32cb62"
    )
    assert len(catalog.objects) == 4
    assert len(catalog.fields) == 43
    assert len(catalog.actions) == 16


def test_historical_relation_catalog_keeps_its_original_identity():
    catalog = catalogs.relation_catalog()
    assert len(catalog.objects) == 7
    assert catalog.digest == catalogs.relation_catalog().digest
    assert len(catalog.fields) == 97
    assert len(catalog.actions) == 45
    assert catalogs.CATALOG_REVISION == "system-fields/3-relations"


async def seed_four(plain):
    old = catalogs.ordinary_catalog()
    async with plain() as db:
        await migrate_legacy(
            db,
            fence=await fence(db),
            catalog=old,
            catalog_revision="system-fields/2-ordinary",
            source_build="b" * 40,
        )
        await db.commit()
    return old


async def snapshot(db):
    tables = [
        models.FieldCatalogSnapshot.__table__,
        models.FieldCatalogObject.__table__,
        models.FieldPolicyHead.__table__,
        models.FieldPolicyObjectState.__table__,
        models.FieldPolicyRevision.__table__,
        models.FieldPolicyBinding.__table__,
        models.FieldPolicyManifest.__table__,
        models.FieldPolicyManifestItem.__table__,
        UserFieldPolicyState.__table__,
    ]
    return {
        t.name: (await db.execute(select(t).order_by(*t.primary_key.columns))).all()
        for t in tables
    }


async def test_four_to_current_preserves_latest_rules_and_history_for_both_tenants(
    system_sessions,
):
    factory, plain = system_sessions
    old = await seed_four(plain)
    policies = factory, plain, old
    for version, effect in ((1, "allow"), (2, "deny")):
        rules = rules_document(
            old,
            (
                Grant(
                    0,
                    "201",
                    FieldRef("system.dict_type", "dict_name"),
                    Access("read", "management.read"),
                    effect,
                ),
            ),
            tenant_id=0,
            role_id=201,
            object_id="system.dict_type",
        )
        await run(
            policies,
            command(
                "publish",
                version,
                role_id=201,
                object_id="system.dict_type",
                binding_version=version - 1,
                rules=rules,
            ),
        )
    await run(policies, command("build", 3))
    async with plain() as db:
        before = await snapshot(db)
        binding_before = (
            (await db.execute(select(models.FieldPolicyBinding.__table__)))
            .mappings()
            .one()
        )
        previous = await db.get(
            models.FieldPolicyRevision, binding_before["revision_id"]
        )
        old_rules, old_digest, old_actor = (
            previous.rules,
            previous.rules_digest,
            previous.actor_user_id,
        )
        epochs = dict(
            (
                await db.execute(
                    select(UserFieldPolicyState.tenant_id, UserFieldPolicyState.epoch)
                )
            ).all()
        )
        await db.rollback()
        result = await prepare_catalog(db, fence=await fence(db), source_build="b" * 40)
        await db.commit()
        assert result["sourceCatalogDigest"] == old.digest
        assert result["changedTenants"] == ["0", "1"]
        assert (
            len((await db.scalars(select(models.FieldPolicyObjectState))).all()) == 28
        )
        binding = (
            (await db.execute(select(models.FieldPolicyBinding.__table__)))
            .mappings()
            .one()
        )
        assert binding["binding_version"] == binding_before["binding_version"] + 1
        new_revision = await db.get(models.FieldPolicyRevision, binding["revision_id"])
        assert (
            new_revision.rules == old_rules and new_revision.rules_digest == old_digest
        )
        assert new_revision.actor_user_id == old_actor
        assert new_revision.rules[0]["effect"] == "deny"
        for table in (
            models.FieldPolicyRevision.__table__,
            models.FieldPolicyManifest.__table__,
            models.FieldPolicyManifestItem.__table__,
        ):
            rows = (
                await db.execute(select(table).order_by(*table.primary_key.columns))
            ).all()
            assert all(row in rows for row in before[table.name])
        assert all(
            b.object_id == "system.dict_type"
            for b in (await db.scalars(select(models.FieldPolicyBinding))).all()
        )
        for head in (await db.scalars(select(models.FieldPolicyHead))).all():
            assert head.catalog_digest == catalogs.production_catalog().digest
            assert (
                head.candidate_manifest_id is None and head.active_manifest_id is None
            )
            assert (
                head.ready_manifest_digest is None and head.qualification_digest is None
            )
        assert dict(
            (
                await db.execute(
                    select(UserFieldPolicyState.tenant_id, UserFieldPolicyState.epoch)
                )
            ).all()
        ) == {tid: epoch + 1 for tid, epoch in epochs.items()}
        upgraded = await snapshot(db)
        await db.rollback()
        replay = await prepare_catalog(db, fence=await fence(db), source_build="b" * 40)
        await db.commit()
        assert replay["changedTenants"] == [] and replay["revisions"] == []
        assert await snapshot(db) == upgraded


async def test_four_to_seven_rolls_back_every_tenant_on_a_late_failure(
    system_sessions, monkeypatch
):
    _, plain = system_sessions
    await seed_four(plain)
    original = policies_service._objects

    async def fail_late(db, tenant_id, catalog):
        if tenant_id == 1:
            raise RuntimeError("second tenant fault")
        return await original(db, tenant_id, catalog)

    monkeypatch.setattr(policies_service, "_objects", fail_late)
    async with plain() as db:
        before = await snapshot(db)
        await db.rollback()
        with pytest.raises(RuntimeError, match="second tenant fault"):
            await prepare_catalog(db, fence=await fence(db), source_build="b" * 40)
        await db.rollback()
        assert await snapshot(db) == before


@pytest.mark.parametrize("frozen", [False, True])
async def test_previously_active_four_object_heads_cannot_be_upgraded(
    system_sessions, monkeypatch, frozen
):
    factory, plain = system_sessions
    old = await seed_four(plain)
    monkeypatch.setattr(settings, "RELEASE_BUILD_SHA", "b" * 40)
    proof = Qualification(
        "b" * 40,
        old.digest,
        *catalog_parts(old),
        "c" * 64,
        datetime.now(UTC) + timedelta(minutes=30),
    )
    policies = factory, plain, old
    built = await run(policies, command("build", 1))
    marked = await run(
        policies,
        command("ready", 2, expected_epoch=built["epoch"]),
        qualification=proof,
    )
    activated = await run(
        policies,
        command("activate", 3, expected_epoch=marked["epoch"]),
        qualification=proof,
    )
    if frozen:
        await run(
            policies,
            command(
                "freeze",
                4,
                expected_epoch=activated["epoch"],
                object_id="system.dict_type",
            ),
        )
    async with plain() as db:
        before = await snapshot(db)
        await db.rollback()
        with pytest.raises(BusinessException):
            await prepare_catalog(db, fence=await fence(db), source_build="b" * 40)
        await db.rollback()
        assert await snapshot(db) == before


async def test_new_install_can_prepare_current_objects_without_a_staging_head(
    system_sessions,
):
    _, plain = system_sessions
    async with plain() as db:
        result = await prepare_catalog(db, fence=await fence(db), source_build="b" * 40)
        await db.commit()
        assert result["catalogDigest"] == catalogs.production_catalog().digest
        heads = (await db.scalars(select(models.FieldPolicyHead))).all()
        assert {h.tenant_id for h in heads} == {0, 1}
        assert all(
            h.head_version == 1 and h.rollout_state == "preparing" for h in heads
        )
        assert (
            len((await db.scalars(select(models.FieldPolicyObjectState))).all()) == 28
        )


@pytest.mark.parametrize("change", ["original", "new_read", "unsourced"])
async def test_legacy_migration_rejects_user_contract_changes(system_sessions, change):
    _, plain = system_sessions
    user = Catalog((user_compat_object(),), ()).objects["system.user"]
    if change == "original":
        user = replace(
            user,
            fields=tuple(
                replace(f, nullable=not f.nullable) if f.field_id == "nickname" else f
                for f in user.fields
            ),
        )
    else:
        extra = FieldSpec(
            "unexpected",
            "string",
            (Access("read", "management.read"),) if change == "new_read" else (),
        )
        user = replace(user, fields=user.fields + (extra,))
    catalog = Catalog((user,), ())
    async with plain() as db:
        before = await snapshot(db)
        await db.rollback()
        with pytest.raises(BusinessException):
            await migrate_legacy(
                db,
                fence=await fence(db),
                catalog=catalog,
                catalog_revision="test/invalid",
                source_build="b" * 40,
            )
        await db.rollback()
        assert await snapshot(db) == before


async def test_preparation_rejects_unknown_source_catalog_without_changes(
    system_sessions,
):
    _, plain = system_sessions
    user = user_compat_object()
    unknown = Catalog(
        (
            user,
            replace(
                catalogs.ordinary_catalog().objects["system.dict_type"],
                scope_adapter="dict-scope/unknown",
            ),
        ),
        (),
    )
    async with plain() as db:
        await migrate_legacy(
            db,
            fence=await fence(db),
            catalog=unknown,
            catalog_revision="test/unknown",
            source_build="b" * 40,
        )
        await db.commit()
        before = await snapshot(db)
        await db.rollback()
        with pytest.raises(BusinessException):
            await prepare_catalog(db, fence=await fence(db), source_build="b" * 40)
        await db.rollback()
        assert await snapshot(db) == before


@pytest.mark.parametrize("invalid", ["mixed", "missing-head"])
async def test_preparation_rejects_an_incomplete_or_mixed_tenant_set(
    system_sessions, invalid
):
    _, plain = system_sessions
    await seed_four(plain)
    async with plain() as db:
        if invalid == "mixed":
            catalog = catalogs.production_catalog()
            await register_catalog(
                db,
                fence=await fence(db),
                catalog=catalog,
                catalog_revision=catalogs.CATALOG_REVISION,
            )
            await db.execute(
                update(models.FieldPolicyHead)
                .where(models.FieldPolicyHead.tenant_id == 1)
                .values(
                    catalog_digest=catalog.digest,
                    candidate_manifest_id=None,
                    candidate_manifest_digest=None,
                )
            )
        else:
            await db.execute(
                insert(Tenant).values(
                    tenant_id=2,
                    tenant_code="plan-2",
                    tenant_name="Unprepared",
                    status="1",
                    lifecycle_state="active",
                    bootstrap_version=1,
                    row_version=1,
                    bootstrap_key_hash="d" * 64,
                    bootstrap_fingerprint="e" * 64,
                )
            )
            # Tenant insertion creates its barrier through the real DB trigger.
        await db.commit()
        before = await snapshot(db)
        await db.rollback()
        with pytest.raises(BusinessException):
            await prepare_catalog(db, fence=await fence(db), source_build="b" * 40)
        await db.rollback()
        assert await snapshot(db) == before
