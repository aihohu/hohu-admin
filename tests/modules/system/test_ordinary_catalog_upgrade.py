"""Stopped catalog expansion preserves candidate rules and invalidates readiness."""

import pytest
from sqlalchemy import select

from app.core.exceptions import BusinessException
from app.core.field_policy import Access, Catalog, FieldRef, Grant
from app.core.field_policy.documents import rules_document
from app.modules.system.field_policy.generic_compat import user_compat_object
from app.modules.system.field_policy.ordinary_catalog import production_catalog
from app.modules.system.models.field_policy import (
    FieldPolicyBinding,
    FieldPolicyHead,
    FieldPolicyObjectState,
    FieldPolicyRevision,
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


async def test_expand_catalog_is_atomic_and_replay_does_not_increment_versions(
    system_sessions,
):

    _, plain = system_sessions
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
        result = await prepare_catalog(db, fence=await fence(db), source_build="b" * 40)
        await db.commit()
        head = await db.get(FieldPolicyHead, 0)
        assert head.catalog_digest == production_catalog().digest
        assert head.rollout_state == "preparing" and head.active_manifest_id is None
        assert head.candidate_manifest_id is None and head.head_version == 2
        assert (
            len(
                (
                    await db.scalars(
                        select(FieldPolicyObjectState).where(
                            FieldPolicyObjectState.tenant_id == 0
                        )
                    )
                ).all()
            )
            == 4
        )
        await db.rollback()
        again = await prepare_catalog(db, fence=await fence(db), source_build="b" * 40)
        await db.commit()
        assert again["catalogDigest"] == result["catalogDigest"]
        assert (await db.get(FieldPolicyHead, 0)).head_version == 2


async def test_catalog_expansion_preserves_latest_edits_and_invalidates_old_cas(
    system_sessions,
):
    factory, plain = system_sessions
    old = Catalog((user_compat_object(),), ())
    async with plain() as db:
        await migrate_legacy(
            db,
            fence=await fence(db),
            catalog=old,
            catalog_revision="test/old",
            source_build="b" * 40,
        )
        await db.commit()
    policies = factory, plain, old
    published = None
    for version, effect in ((1, "allow"), (2, "deny")):
        rules = rules_document(
            old,
            (
                Grant(
                    0,
                    "201",
                    FieldRef("system.user", "nickname"),
                    Access("read", "management.read"),
                    effect,
                ),
            ),
            tenant_id=0,
            role_id=201,
            object_id="system.user",
        )
        published = await run(
            policies,
            command(
                "publish",
                version,
                role_id=201,
                object_id="system.user",
                binding_version=version - 1,
                rules=rules,
            ),
        )
    await run(policies, command("build", 3))
    async with plain() as db:
        old_revision_id = int(published["revisionId"])
        previous = await db.get(FieldPolicyRevision, old_revision_id)
        old_rules, old_actor = previous.rules, previous.actor_user_id
        await db.rollback()
        result = await prepare_catalog(db, fence=await fence(db), source_build="b" * 40)
        await db.commit()
        binding = await db.get(FieldPolicyBinding, (0, 201, "system.user"))
        assert binding.binding_version == 3 and binding.revision_id != old_revision_id
        latest = await db.get(FieldPolicyRevision, binding.revision_id)
        historical = await db.get(FieldPolicyRevision, old_revision_id)
        assert latest.rules == historical.rules == old_rules
        assert latest.actor_user_id == old_actor
        assert latest.template_revision == f"catalog-upgrade/{old_revision_id}"
        head = await db.get(FieldPolicyHead, 0)
        assert head.head_version == 5 and head.candidate_manifest_id is None
        assert result["revisions"] == [
            {
                "oldRevision": str(old_revision_id),
                "newRevision": str(binding.revision_id),
            }
        ]
        assert (await db.scalars(select(FieldPolicyBinding))).all() == [binding]
    with pytest.raises(BusinessException):
        await run((factory, plain, production_catalog()), command("build", 4))
