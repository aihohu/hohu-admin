"""The controlled catalog expands exact historical sources without new grants."""

import pytest
from sqlalchemy import func, select, update

from app.core.exceptions import BusinessException
from app.modules.system.field_policy.control_catalog import control_catalog
from app.modules.system.field_policy.ordinary_catalog import (
    ordinary_catalog,
    production_catalog,
    relation_catalog,
)
from app.modules.system.models.field_policy import (
    FieldCatalogSnapshot,
    FieldPolicyBinding,
    FieldPolicyHead,
    FieldPolicyObjectState,
    FieldPolicyRevision,
)
from app.modules.system.models.user_field_policy import UserFieldPolicyState
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


def test_production_catalog_keeps_the_immutable_ten_object_source():
    assert set(control_catalog().objects) <= production_catalog().objects.keys()
    assert len(control_catalog().objects) == 10
    assert (
        ordinary_catalog().digest
        == "1bba4c34c8f8e6edd636ed8b2447f8fbd0053e7082670846aa11ab25db32cb62"
    )
    assert (
        relation_catalog().digest
        == "0cc4f21cfd52b83d1ee018a2009034cf546d437d93074716bfdb80c82e3d0ec6"
    )


@pytest.mark.parametrize("source", [ordinary_catalog(), relation_catalog()])
async def test_historical_catalog_expands_both_tenants_and_replays_without_epoch(
    source, system_sessions
):
    _, plain = system_sessions
    async with plain() as db:
        await migrate_legacy(
            db,
            fence=await fence(db),
            catalog=source,
            catalog_revision="test/historical",
            source_build="b" * 40,
        )
        await db.commit()
        epochs = dict(
            (
                await db.execute(
                    select(UserFieldPolicyState.tenant_id, UserFieldPolicyState.epoch)
                )
            ).all()
        )
        await db.rollback()
        first = await prepare_catalog(db, fence=await fence(db), source_build="b" * 40)
        await db.commit()
        assert first["changedTenants"] == ["0", "1"]
        for head in await db.scalars(select(FieldPolicyHead)):
            assert (
                head.catalog_digest == production_catalog().digest
                and head.head_version == 2
            )
        for state in await db.scalars(select(UserFieldPolicyState)):
            assert state.epoch == epochs[state.tenant_id] + 1
        rows = (
            await db.execute(
                select(
                    FieldPolicyObjectState.tenant_id, FieldPolicyObjectState.object_id
                )
            )
        ).all()
        assert len(rows) == 2 * len(production_catalog().objects)
        await db.rollback()
        replay = await prepare_catalog(db, fence=await fence(db), source_build="b" * 40)
        await db.commit()
        assert replay["changedTenants"] == [] and replay["revisions"] == []
        assert all(
            head.head_version == 2 for head in await db.scalars(select(FieldPolicyHead))
        )


async def test_invalid_later_tenant_leaves_no_partial_migration_even_if_caught(
    system_sessions,
):
    _, plain = system_sessions
    source = relation_catalog()
    async with plain() as db:
        await migrate_legacy(
            db,
            fence=await fence(db),
            catalog=source,
            catalog_revision="test/historical",
            source_build="b" * 40,
        )
        await db.commit()
        await db.execute(
            update(FieldPolicyObjectState)
            .where(
                FieldPolicyObjectState.tenant_id == 1,
                FieldPolicyObjectState.object_id == "system.user",
            )
            .values(mode="frozen", ever_enforced=True, active_object_digest="a" * 64)
        )
        await db.commit()
        with pytest.raises(BusinessException):
            await prepare_catalog(db, fence=await fence(db), source_build="b" * 40)
        await db.commit()
        assert (await db.get(FieldPolicyHead, 0)).catalog_digest == source.digest
        assert await db.get(FieldCatalogSnapshot, production_catalog().digest) is None


async def test_fault_after_clone_rolls_back_even_if_caller_commits(
    system_sessions, monkeypatch
):
    factory, plain = system_sessions
    source = relation_catalog()
    async with plain() as db:
        await migrate_legacy(
            db,
            fence=await fence(db),
            catalog=source,
            catalog_revision="test/historical",
            source_build="b" * 40,
        )
        await db.commit()
    await run(
        (factory, plain, source),
        command(
            "publish",
            1,
            role_id=201,
            object_id="system.user",
            binding_version=0,
            rules=[],
        ),
    )
    async with plain() as db:
        original_binding = (
            (await db.execute(select(FieldPolicyBinding.__table__))).mappings().one()
        )
        count = await db.scalar(select(func.count()).select_from(FieldPolicyRevision))
        await db.rollback()
        execute = db.execute

        async def fault(statement, *args, **kwargs):
            result = await execute(statement, *args, **kwargs)
            if (
                getattr(statement, "is_update", False)
                and statement.table.name == "sys_field_policy_head"
            ):
                raise RuntimeError("injected after the head mutation")
            return result

        monkeypatch.setattr(db, "execute", fault)
        with pytest.raises(RuntimeError):
            await prepare_catalog(db, fence=await fence(db), source_build="b" * 40)
        await db.commit()
        monkeypatch.setattr(db, "execute", execute)
        assert (
            await db.execute(select(FieldPolicyBinding.__table__))
        ).mappings().one() == original_binding
        assert (
            await db.scalar(select(func.count()).select_from(FieldPolicyRevision))
            == count
        )
        assert (await db.get(FieldPolicyHead, 0)).catalog_digest == source.digest
        assert await db.get(FieldCatalogSnapshot, production_catalog().digest) is None
