"""Consumer fixtures prepare candidates without replaying every publish command."""

from contextlib import asynccontextmanager

from sqlalchemy import insert, select, text

from app.core.field_policy import (
    Access,
    Catalog,
    FieldRef,
    FieldSpec,
    Grant,
    ObjectSpec,
)
from app.db.session import engine
from app.modules.system.field_policy.relation_catalog import READ
from app.modules.system.models.field_policy import (
    FieldPolicyAudit,
    FieldPolicyBinding,
    FieldPolicyHead,
    FieldPolicyObjectState,
    FieldPolicyRevision,
)
from app.modules.system.models.role import Role
from tests.modules.system import test_relation_field_writes as helper
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


async def test_consumer_preparation_runs_only_real_activation_commands(
    system_sessions, monkeypatch
):
    seen = []
    original = helper.policy_command

    async def observed(policies, command, **kwargs):
        seen.append(command["action"])
        return await original(policies, command, **kwargs)

    monkeypatch.setattr(helper, "policy_command", observed)
    await helper.activate_relations(
        system_sessions,
        monkeypatch,
        missing=(("system.role", "role_name", "read"),),
    )
    assert seen == ["build", "ready", "activate"]
    _, plain = system_sessions
    async with plain() as db:
        head = await db.get(FieldPolicyHead, 0)
        assert head.active_manifest_digest == head.candidate_manifest_digest
        objects = (await db.scalars(select(FieldPolicyObjectState))).all()
        assert len(objects) == len(helper.relation_catalog().objects)
        assert all(obj.mode == "enforcing" for obj in objects)
        bindings = (await db.scalars(select(FieldPolicyBinding))).all()
        assert len(bindings) == len(objects)
        for binding in bindings:
            revision = await db.get(FieldPolicyRevision, binding.revision_id)
            assert revision.catalog_digest == helper.relation_catalog().digest
            assert revision.revision_no == binding.binding_version == 1
            if binding.object_id == "system.role":
                assert not any(
                    rule["field_id"] == "role_name"
                    and rule["access"]["operation"] == "read"
                    for rule in revision.rules
                )
        assert set(await db.scalars(select(FieldPolicyAudit.action))) == {
            "build",
            "ready",
            "activate",
        }


async def test_prepared_candidates_match_real_publication(monkeypatch):
    """Separate real schemas compare all rules, including omission and extra role.

    IDs, epochs and audit history intentionally differ: a fixture seed does not
    claim to be a publication. Both paths must build and activate real manifests.
    """
    snapshots = []
    schemas = []
    for publish_candidates in (True, False):
        async with asynccontextmanager(plan_sessions.__wrapped__)() as sessions:
            await system_sessions.__wrapped__(sessions)
            _, plain = sessions
            async with plain() as db:
                schemas.append(await db.scalar(text("SELECT current_schema()")))
                await db.execute(
                    insert(Role).values(
                        tenant_id=0,
                        role_id=501,
                        role_name="extra",
                        role_code="EXTRA",
                        status="1",
                        data_scope="5",
                    )
                )
                await db.commit()
            await helper.activate_relations(
                sessions,
                monkeypatch,
                missing=(("system.role", "role_name", "read"),),
                extra_grants=(
                    Grant(0, "501", FieldRef("system.role", "role_name"), READ),
                ),
                publish_candidates=publish_candidates,
            )
            async with plain() as db:
                bindings = (await db.scalars(select(FieldPolicyBinding))).all()
                content = {}
                for binding in bindings:
                    revision = await db.get(FieldPolicyRevision, binding.revision_id)
                    content[binding.role_id, binding.object_id] = (
                        revision.catalog_digest,
                        revision.rules,
                        revision.rules_digest,
                        revision.revision_no,
                        binding.binding_version,
                        binding.archived_at,
                    )
                objects = (await db.scalars(select(FieldPolicyObjectState))).all()
                assert all(o.mode == "enforcing" and o.ever_enforced for o in objects)
                head = await db.get(FieldPolicyHead, 0)
                assert head.active_manifest_digest == head.candidate_manifest_digest
                snapshots.append(content)
    assert snapshots[0] == snapshots[1]
    assert (501, "system.role") in snapshots[0]
    assert len(set(schemas)) == 2
    async with engine.connect() as db:
        for schema in schemas:
            assert not await db.scalar(
                text("SELECT EXISTS(SELECT 1 FROM pg_namespace WHERE nspname=:name)"),
                {"name": schema},
            )


async def test_preparation_keeps_custom_catalog_and_rule_transform(
    system_sessions, monkeypatch
):
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
    original = helper.rules_document

    def empty_rules(catalog, _grants, **kwargs):
        return original(catalog, (), **kwargs)

    monkeypatch.setattr(helper, "rules_document", empty_rules)
    await helper.activate_relations(system_sessions, monkeypatch, catalog=catalog)
    _, plain = system_sessions
    async with plain() as db:
        revisions = (await db.scalars(select(FieldPolicyRevision))).all()
        assert len(revisions) == 1
        assert revisions[0].rules == []
        assert revisions[0].object_id == "sample.item"
        assert revisions[0].catalog_digest == catalog.digest
