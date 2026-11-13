"""Ten-object preparation expands atomically without granting file fields."""

from sqlalchemy import select

from app.modules.system.field_policy.control_catalog import control_catalog
from app.modules.system.field_policy.ordinary_catalog import production_catalog
from app.modules.system.field_policy.transfer_catalog import transfer_catalog
from app.modules.system.models.field_policy import (
    FieldPolicyBinding,
    FieldPolicyHead,
    FieldPolicyObjectState,
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


def test_current_preparation_catalog_is_the_complete_transfer_catalog():
    assert production_catalog().digest == transfer_catalog().digest


async def test_ten_to_fourteen_upgrade_has_empty_new_bindings_and_safe_replay(
    system_sessions,
):
    _, plain = system_sessions
    async with plain() as db:
        await migrate_legacy(
            db,
            fence=await fence(db),
            catalog=control_catalog(),
            catalog_revision="test/controls",
            source_build="b" * 40,
        )
        await db.commit()
        result = await prepare_catalog(db, fence=await fence(db), source_build="b" * 40)
        await db.commit()
        assert result["changedTenants"] == ["0", "1"]
        heads = list(await db.scalars(select(FieldPolicyHead)))
        assert all(h.catalog_digest == transfer_catalog().digest for h in heads)
        states = list(await db.scalars(select(FieldPolicyObjectState)))
        assert len(states) == 28
        new = set(transfer_catalog().objects) - set(control_catalog().objects)
        assert all(
            s.mode == "legacy" and not s.ever_enforced
            for s in states
            if s.object_id in new
        )
        assert not list(
            await db.scalars(
                select(FieldPolicyBinding).where(FieldPolicyBinding.object_id.in_(new))
            )
        )
        await db.rollback()
        replay = await prepare_catalog(db, fence=await fence(db), source_build="b" * 40)
        assert replay["changedTenants"] == []
        await db.commit()
