"""Generic policy DDL invariants tested against actual PostgreSQL constraints."""

import asyncio
import runpy
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import DateTime, insert, select, update
from sqlalchemy.exc import DBAPIError

from app.core.tenant_inventory import PLATFORM_GLOBAL_TABLES, TENANT_MODEL_INVENTORY
from app.modules.system.models.field_policy import (
    FieldCatalogObject,
    FieldCatalogSnapshot,
    FieldPolicyAudit,
    FieldPolicyBinding,
    FieldPolicyHead,
    FieldPolicyManifest,
    FieldPolicyManifestItem,
    FieldPolicyMigrationMap,
    FieldPolicyObjectState,
    FieldPolicyRequest,
    FieldPolicyRevision,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as _plan_sessions,
)

plan_sessions = _plan_sessions

MODELS = (
    FieldCatalogSnapshot,
    FieldCatalogObject,
    FieldPolicyHead,
    FieldPolicyObjectState,
    FieldPolicyRevision,
    FieldPolicyBinding,
    FieldPolicyManifest,
    FieldPolicyManifestItem,
    FieldPolicyAudit,
    FieldPolicyRequest,
    FieldPolicyMigrationMap,
)
D = "a" * 64


def install(connection):
    path = (
        Path(__file__).resolve().parents[3]
        / "alembic/versions/e020f1200001_generic_field_policy.py"
    )
    with Operations.context(MigrationContext.configure(connection)):
        runpy.run_path(str(path))["upgrade"]()


@pytest.fixture
async def generic_sessions(plan_sessions):
    factory, plain = plan_sessions
    yield factory, plain


def test_new_tables_have_utc_time_and_tenant_inventory():
    assert len(MODELS) == 11
    for model in MODELS:
        table = model.__table__
        assert (
            table.name in TENANT_MODEL_INVENTORY or table.name in PLATFORM_GLOBAL_TABLES
        )
        for column in table.c:
            if isinstance(column.type, DateTime):
                assert column.type.timezone


async def seed(db):
    await db.execute(
        insert(FieldCatalogSnapshot).values(
            catalog_digest=D,
            schema_version=2,
            catalog_revision="test/1",
            action_catalog_digest=D,
            adapter_digest=D,
            canonical_document={"schema_version": 2, "objects": [], "actions": []},
        )
    )
    await db.execute(
        insert(FieldCatalogObject).values(
            catalog_digest=D,
            object_id="sample.item",
            domain_kind="tenant",
            schema_digest=D,
            adapter_version="sample.scope",
        )
    )
    await db.execute(insert(FieldPolicyHead).values(tenant_id=0, catalog_digest=D))
    await db.execute(
        insert(FieldPolicyObjectState).values(
            tenant_id=0, object_id="sample.item", catalog_digest=D
        )
    )
    await db.execute(
        insert(FieldPolicyRevision).values(
            revision_id=301,
            tenant_id=0,
            role_id=201,
            object_id="sample.item",
            revision_no=1,
            catalog_digest=D,
            rules=[],
            rules_digest=D,
            actor_user_id=101,
        )
    )


@pytest.mark.parametrize(
    "changes", [{"tenant_id": 1}, {"role_id": 202}, {"object_id": "system.user"}]
)
async def test_binding_composite_fk_rejects_cross_identity(generic_sessions, changes):
    _, plain = generic_sessions
    async with plain() as db:
        await seed(db)
        with pytest.raises(DBAPIError):
            async with db.begin_nested():
                await db.execute(
                    insert(FieldPolicyBinding).values(
                        **(
                            {
                                "tenant_id": 0,
                                "role_id": 201,
                                "object_id": "sample.item",
                                "revision_id": 301,
                                "binding_version": 1,
                            }
                            | changes
                        )
                    )
                )


@pytest.mark.parametrize(
    "model,changes",
    [
        (FieldCatalogSnapshot, {"catalog_revision": "changed"}),
        (FieldCatalogObject, {"adapter_version": "changed"}),
        (FieldPolicyRevision, {"rules": []}),
    ],
)
async def test_immutable_facts_reject_even_noop_update(
    generic_sessions, model, changes
):
    _, plain = generic_sessions
    async with plain() as db:
        await seed(db)
        with pytest.raises(DBAPIError):
            async with db.begin_nested():
                await db.execute(update(model).values(**changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"rollout_state": "enforcing"},
        {"active_manifest_id": 401},
        {"ready_manifest_digest": D},
        {"head_version": 0},
    ],
)
async def test_head_rejects_partial_state(generic_sessions, changes):
    _, plain = generic_sessions
    async with plain() as db:
        await seed(db)
        with pytest.raises(DBAPIError):
            async with db.begin_nested():
                await db.execute(
                    update(FieldPolicyHead)
                    .where(FieldPolicyHead.tenant_id == 0)
                    .values(**changes)
                )


async def test_sealed_manifest_forbids_late_child(generic_sessions):
    _, plain = generic_sessions
    async with plain() as db:
        await seed(db)
        await db.execute(
            insert(FieldPolicyManifest).values(
                manifest_id=401,
                tenant_id=0,
                manifest_version=1,
                catalog_digest=D,
                action_catalog_digest=D,
                adapter_digest=D,
                digest=D,
                expected_item_count=0,
                created_by=101,
            )
        )
        await db.execute(
            update(FieldPolicyManifest)
            .where(FieldPolicyManifest.manifest_id == 401)
            .values(status="sealed")
        )
        with pytest.raises(DBAPIError):
            async with db.begin_nested():
                await db.execute(
                    insert(FieldPolicyManifestItem).values(
                        manifest_id=401,
                        tenant_id=0,
                        role_id=201,
                        object_id="sample.item",
                        revision_id=301,
                        catalog_digest=D,
                        rules_digest=D,
                    )
                )
        assert await db.scalar(select(FieldPolicyManifest.status)) == "sealed"


async def test_concurrent_seal_serializes_child_insert(generic_sessions):
    _, plain = generic_sessions
    async with plain() as db:
        await seed(db)
        await db.execute(
            insert(FieldPolicyManifest).values(
                manifest_id=401,
                tenant_id=0,
                manifest_version=1,
                catalog_digest=D,
                action_catalog_digest=D,
                adapter_digest=D,
                digest=D,
                expected_item_count=0,
                created_by=101,
            )
        )
        await db.commit()
    async with plain() as sealer, plain() as inserter:
        await sealer.execute(
            update(FieldPolicyManifest)
            .where(FieldPolicyManifest.manifest_id == 401)
            .values(status="sealed")
        )
        pending = asyncio.create_task(
            inserter.execute(
                insert(FieldPolicyManifestItem).values(
                    manifest_id=401,
                    tenant_id=0,
                    role_id=201,
                    object_id="sample.item",
                    revision_id=301,
                    catalog_digest=D,
                    rules_digest=D,
                )
            )
        )
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(pending), 0.1)
            await sealer.commit()
            with pytest.raises(DBAPIError):
                await asyncio.wait_for(pending, 3)
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            await inserter.rollback()


async def test_downgrade_refuses_persisted_generic_facts(generic_sessions):
    _, plain = generic_sessions
    async with plain() as db:
        await seed(db)
        with pytest.raises(DBAPIError):
            async with db.begin_nested():

                def downgrade(connection):
                    path = (
                        Path(__file__).resolve().parents[3]
                        / "alembic/versions/e020f1200001_generic_field_policy.py"
                    )
                    with Operations.context(MigrationContext.configure(connection)):
                        runpy.run_path(str(path))["downgrade"]()

                await (await db.connection()).run_sync(downgrade)
        assert await db.scalar(select(FieldPolicyHead.tenant_id)) == 0


@pytest.mark.parametrize(
    "change", [{"role_id": 202}, {"object_id": "system.user"}, {"revision_id": None}]
)
async def test_receipt_revision_reference_cannot_cross_identity(
    generic_sessions, change
):
    _, plain = generic_sessions
    async with plain() as db:
        await seed(db)
        with pytest.raises(DBAPIError):
            async with db.begin_nested():
                await db.execute(
                    insert(FieldPolicyRequest).values(
                        **(
                            {
                                "tenant_id": 0,
                                "actor_user_id": 101,
                                "request_id": "11111111-1111-1111-1111-111111111111",
                                "command_digest": D,
                                "receipt": {},
                                "receipt_digest": D,
                                "role_id": 201,
                                "object_id": "sample.item",
                                "revision_id": 301,
                            }
                            | change
                        )
                    )
                )
