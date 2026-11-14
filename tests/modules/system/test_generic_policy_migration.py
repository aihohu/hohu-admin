"""Legacy history is converted only under maintenance and never erased."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, select, update

from app.core.authorization_maintenance import (
    StoppedWritersEvidence,
    acquire_maintenance_fence,
)
from app.core.config import settings
from app.core.exceptions import BusinessException
from app.core.field_policy import Catalog
from app.core.field_policy.documents import catalog_parts, load_rules
from app.core.field_policy.qualification import Qualification
from app.modules.system.field_policy.documents import (
    canonical_rules,
    catalog_digest,
    document_digest,
    rules_digest,
)
from app.modules.system.field_policy.evaluator import FieldGrant
from app.modules.system.field_policy.generic_compat import (
    convert_legacy_grants,
    user_compat_object,
)
from app.modules.system.models.field_policy import (
    FieldPolicyHead,
    FieldPolicyMigrationMap,
    FieldPolicyObjectState,
    FieldPolicyRevision,
)
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyBinding,
    UserFieldPolicyRevision,
    UserFieldPolicyState,
)
from app.modules.system.service.generic_field_policy_migration import migrate_legacy
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions,  # noqa: F401
)
from tests.modules.system.test_generic_policy_persistence import run
from tests.modules.system.test_generic_policy_schema import (
    generic_sessions as _generic_sessions,
)
from tests.modules.system.test_generic_policy_transitions import command

generic_sessions = _generic_sessions

BUILD = "b" * 40


async def test_identical_migration_replay_verifies_without_new_facts(generic_sessions):
    _, sessions = generic_sessions
    catalog = Catalog((user_compat_object(),), ())
    async with sessions() as db:
        await legacy(db)
        first = await migrate_legacy(
            db,
            fence=await fence(db),
            catalog=catalog,
            catalog_revision="test/1",
            source_build=BUILD,
        )
        await db.commit()
        before = (await db.execute(select(FieldPolicyMigrationMap.__table__))).all()
        await db.rollback()
        second = await migrate_legacy(
            db,
            fence=await fence(db),
            catalog=catalog,
            catalog_revision="test/1",
            source_build=BUILD,
        )
        assert second == first
        assert (
            await db.execute(select(FieldPolicyMigrationMap.__table__))
        ).all() == before
        await db.rollback()


async def fence(db):
    async def observer():
        return StoppedWritersEvidence(
            "test",
            "0.2.0",
            BUILD,
            "c" * 64,
            datetime.now(UTC),
            frozenset({"api", "worker", "scheduler", "web-write-ingress"}),
            0,
            True,
        )

    return await acquire_maintenance_fence(
        observer, action="migration", expected_build_sha=BUILD, db=db
    )


async def legacy(db, rules=None):
    rules = [] if rules is None else rules
    await db.execute(
        insert(UserFieldPolicyRevision).values(
            revision_id=301,
            tenant_id=0,
            role_id=201,
            revision_no=1,
            catalog_revision="user-fields/1",
            catalog_digest=catalog_digest(),
            rules=rules,
            rules_digest=rules_digest(rules),
            created_by_user_id=101,
        )
    )
    await db.execute(
        insert(UserFieldPolicyBinding).values(
            tenant_id=0, role_id=201, revision_id=301, version=1
        )
    )
    await db.execute(
        update(UserFieldPolicyState)
        .where(UserFieldPolicyState.tenant_id == 0)
        .values(
            epoch=2,
            candidate_manifest_digest=document_digest(
                {
                    "catalog_digest": catalog_digest(),
                    "bindings": [
                        {
                            "role_id": "201",
                            "revision_id": "301",
                            "version": 1,
                            "archived": False,
                            "rules_digest": rules_digest(rules),
                        }
                    ],
                }
            ),
        )
    )
    await db.commit()


@pytest.mark.parametrize("mode", ["legacy", "ready", "enforcing", "frozen"])
async def test_migration_preserves_rule_semantics_and_enforced_history(
    generic_sessions, mode
):
    _, plain = generic_sessions
    catalog = Catalog((user_compat_object(),), ())
    old_grants = (
        FieldGrant("201", "nickname", "read", "management.read"),
        FieldGrant("201", "user_name", "read", "management.read", "deny"),
        FieldGrant(
            "201",
            "user_email",
            "read",
            "management.read",
            "allow",
            "masked",
            "email/v1",
        ),
        FieldGrant("201", "user_email", "exact", "import"),
    )
    async with plain() as db:
        await legacy(db, canonical_rules(old_grants, role_id=201))
        state = await db.get(UserFieldPolicyState, 0)
        active = (
            state.candidate_manifest_digest if mode in {"enforcing", "frozen"} else None
        )
        await db.execute(
            update(UserFieldPolicyState)
            .where(UserFieldPolicyState.tenant_id == 0)
            .values(
                epoch=3,
                mode=mode,
                ever_enforced=active is not None,
                active_manifest_digest=active,
            )
        )
        await db.commit()
        await migrate_legacy(
            db,
            fence=await fence(db),
            catalog=catalog,
            catalog_revision="test/1",
            source_build=BUILD,
        )
        await db.commit()
        revision = (await db.scalars(select(FieldPolicyRevision))).one()
        loaded = load_rules(
            catalog, revision.rules, tenant_id=0, role_id=201, object_id="system.user"
        )
        assert set(loaded) == set(convert_legacy_grants(0, old_grants))
        obj = await db.get(FieldPolicyObjectState, (0, "system.user"))
        assert obj.mode == (mode if active else "legacy")
        assert obj.active_object_digest == active and obj.ever_enforced == (
            active is not None
        )
        assert (await db.get(UserFieldPolicyState, 0)).epoch == 3
        assert (await db.get(FieldPolicyHead, 0)).active_manifest_id is None


async def test_migration_preserves_old_revision_and_default_deny(generic_sessions):
    _, plain = generic_sessions
    async with plain() as db:
        await legacy(db)
        permit = await fence(db)
        catalog = Catalog((user_compat_object(),), ())
        result = await migrate_legacy(
            db,
            fence=permit,
            catalog=catalog,
            catalog_revision="migration/1",
            source_build=BUILD,
        )
        await db.commit()
        assert result["tenants"] == 2
        assert (await db.get(UserFieldPolicyRevision, 301)).rules == []
        revision = (await db.scalars(select(FieldPolicyRevision))).one()
        assert revision.actor_user_id == 101 and revision.rules == []
        assert (await db.get(FieldPolicyHead, 0)).rollout_state == "preparing"
        assert (await db.get(FieldPolicyHead, 0)).active_manifest_id is None
        assert (
            await db.get(FieldPolicyMigrationMap, ("revision", 301))
        ).target_id == revision.revision_id


async def test_migration_without_permit_and_failure_rolls_back(generic_sessions):
    _, plain = generic_sessions
    async with plain() as db:
        await legacy(db)
        catalog = Catalog((user_compat_object(),), ())
        with pytest.raises(BusinessException):
            await migrate_legacy(
                db,
                fence=None,
                catalog=catalog,
                catalog_revision="migration/1",
                source_build=BUILD,
            )
        permit = await fence(db)
        await migrate_legacy(
            db,
            fence=permit,
            catalog=catalog,
            catalog_revision="migration/1",
            source_build=BUILD,
        )
        await db.rollback()
        assert (await db.scalars(select(FieldPolicyHead))).all() == []
        assert (await db.get(UserFieldPolicyRevision, 301)).rules == []


async def test_generic_activation_and_freeze_mirror_original_user_barrier(
    generic_sessions, monkeypatch
):
    factory, plain = generic_sessions
    catalog = Catalog((user_compat_object(),), ())
    async with plain() as db:
        await legacy(db)
        await migrate_legacy(
            db,
            fence=await fence(db),
            catalog=catalog,
            catalog_revision="test/1",
            source_build=BUILD,
        )
        await db.commit()
    # Synthetic qualification exercises atomic storage, not deployment acceptance.
    monkeypatch.setattr(settings, "RELEASE_BUILD_SHA", BUILD)
    proof = Qualification(
        BUILD,
        catalog.digest,
        *catalog_parts(catalog),
        "c" * 64,
        datetime.now(UTC) + timedelta(minutes=5),
    )
    policies = (factory, plain, catalog)
    built = await run(policies, command("build", 1))
    ready = await run(
        policies,
        command("ready", 2, expected_epoch=built["epoch"]),
        qualification=proof,
    )
    active = await run(
        policies,
        command("activate", 3, expected_epoch=ready["epoch"]),
        qualification=proof,
    )
    frozen = await run(
        policies,
        command("freeze", 4, expected_epoch=active["epoch"], object_id="system.user"),
    )
    async with plain() as db:
        old = await db.get(UserFieldPolicyState, 0)
        obj = await db.get(FieldPolicyObjectState, (0, "system.user"))
        head = await db.get(FieldPolicyHead, 0)
        assert old.mode == obj.mode == head.rollout_state == "frozen"
        assert old.ever_enforced and obj.ever_enforced and head.ever_enforced
        assert old.active_manifest_digest == obj.active_object_digest
        assert old.epoch == frozen["epoch"]
        assert head.active_manifest_digest == active["activeManifestDigest"]
