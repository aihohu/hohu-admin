"""Frozen storage shape and PostgreSQL protection; never activates field policy."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import DateTime, delete, func, select, text, update
from sqlalchemy.exc import DBAPIError

from app.core.id_generator import next_id
from app.modules.system.models.role import Role
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyAudit,
    UserFieldPolicyBinding,
    UserFieldPolicyGlobalGuard,
    UserFieldPolicyRevision,
    UserFieldPolicyState,
)
from tests.tenant_helpers import create_test_tenant

CATALOG_DIGEST = "da9fcde4c22e1e48165642a47bcdd340c8b1ebc5f629160e1dab45ff1ca80248"


def test_policy_storage_uses_timezone_aware_timestamps():
    for model in (
        UserFieldPolicyGlobalGuard,
        UserFieldPolicyState,
        UserFieldPolicyRevision,
        UserFieldPolicyBinding,
        UserFieldPolicyAudit,
    ):
        for column in model.__table__.columns:
            if isinstance(column.type, DateTime):
                assert column.type.timezone is True


def test_binding_revision_reference_includes_tenant_and_role():
    constraints = UserFieldPolicyBinding.__table__.foreign_key_constraints
    assert any(
        tuple(constraint.column_keys) == ("tenant_id", "role_id", "revision_id")
        and constraint.ondelete == "RESTRICT"
        for constraint in constraints
    )


def test_global_guard_identity_has_no_automatic_sequence():
    assert UserFieldPolicyGlobalGuard.__table__.c.guard_id.autoincrement is False


@pytest.fixture
async def policy_rows(db_session):
    first = await create_test_tenant(db_session, prefix="schema-a")
    second = await create_test_tenant(db_session, prefix="schema-b")
    roles = [
        Role(
            role_id=next_id(),
            tenant_id=tenant,
            role_name="field-policy-" + str(index),
            role_code="FIELD_POLICY_" + str(index),
            status="1",
        )
        for index, tenant in enumerate(
            [first.tenant_id, first.tenant_id, second.tenant_id]
        )
    ]
    db_session.add_all(roles)
    await db_session.flush()
    revision = UserFieldPolicyRevision(
        revision_id=next_id(),
        tenant_id=first.tenant_id,
        role_id=roles[0].role_id,
        revision_no=1,
        catalog_revision="user-fields/1",
        catalog_digest=CATALOG_DIGEST,
        rules=[],
        rules_digest="a" * 64,
        created_by_user_id=next_id(),
    )
    db_session.add(revision)
    await db_session.flush()
    binding = UserFieldPolicyBinding(
        tenant_id=first.tenant_id,
        role_id=roles[0].role_id,
        revision_id=revision.revision_id,
        version=1,
    )
    audit = UserFieldPolicyAudit(
        tenant_id=first.tenant_id,
        role_id=roles[0].role_id,
        actor_user_id=next_id(),
        action="publish",
        reason="Storage guard regression",
        correlation_id="field-policy-schema",
        after_digest="a" * 64,
        revision_id=revision.revision_id,
        before_version=0,
        after_version=1,
        epoch=2,
    )
    db_session.add_all([binding, audit])
    await db_session.flush()
    return first, second, roles, revision, binding, audit


async def test_new_tenant_has_only_initial_legacy_state(db_session):
    tenant = await create_test_tenant(db_session, prefix="schema-new")
    state = await db_session.get(UserFieldPolicyState, tenant.tenant_id)
    assert state.mode == "legacy"
    assert state.epoch == 1
    assert state.ever_enforced is False
    assert state.catalog_revision == "user-fields/1"
    assert state.catalog_digest == CATALOG_DIGEST
    assert state.candidate_manifest_digest is None
    assert state.active_manifest_digest is None
    assert state.created_at.tzinfo is not None
    for model in (
        UserFieldPolicyRevision,
        UserFieldPolicyBinding,
        UserFieldPolicyAudit,
    ):
        assert not await db_session.scalar(
            select(func.count())
            .select_from(model)
            .where(model.tenant_id == tenant.tenant_id)
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"mode": "enforcing"},
        {"mode": "frozen"},
        {"ever_enforced": True},
        {"active_manifest_digest": "a" * 64},
        {"mode": "ready"},
        {"catalog_digest": "invalid"},
    ],
)
async def test_state_cannot_activate_or_accept_invalid_candidate(
    db_session, policy_rows, changes
):
    first, *_ = policy_rows
    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            await db_session.execute(
                update(UserFieldPolicyState)
                .where(UserFieldPolicyState.tenant_id == first.tenant_id)
                .values(epoch=2, **changes)
            )


async def test_state_change_requires_epoch_and_cannot_delete(db_session, policy_rows):
    first, *_ = policy_rows
    for statement in (
        update(UserFieldPolicyState)
        .where(UserFieldPolicyState.tenant_id == first.tenant_id)
        .values(candidate_manifest_digest="a" * 64),
        update(UserFieldPolicyState)
        .where(UserFieldPolicyState.tenant_id == first.tenant_id)
        .values(epoch=0),
        delete(UserFieldPolicyState).where(
            UserFieldPolicyState.tenant_id == first.tenant_id
        ),
    ):
        with pytest.raises(DBAPIError):
            async with db_session.begin_nested():
                await db_session.execute(statement)
    await db_session.execute(
        update(UserFieldPolicyState)
        .where(UserFieldPolicyState.tenant_id == first.tenant_id)
        .values(mode="ready", candidate_manifest_digest="a" * 64, epoch=2)
    )
    state = await db_session.get(UserFieldPolicyState, first.tenant_id)
    assert state.mode == "ready"
    assert state.ever_enforced is False


async def test_guard_identity_delete_and_epoch_regression_are_rejected(db_session):
    for statement in (
        delete(UserFieldPolicyGlobalGuard),
        update(UserFieldPolicyGlobalGuard).values(guard_id=2),
        update(UserFieldPolicyGlobalGuard).values(epoch=0),
    ):
        with pytest.raises(DBAPIError):
            async with db_session.begin_nested():
                await db_session.execute(statement)
    default = await db_session.scalar(
        text(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_schema = current_schema() "
            "AND table_name = 'sys_user_field_policy_global_guard' "
            "AND column_name = 'guard_id'"
        )
    )
    assert default is None


@pytest.mark.parametrize("cross_tenant", [False, True])
async def test_binding_revision_requires_same_tenant_and_same_role(
    db_session, policy_rows, cross_tenant
):
    _first, _second, roles, revision, *_ = policy_rows
    role = roles[2] if cross_tenant else roles[1]
    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            db_session.add(
                UserFieldPolicyBinding(
                    tenant_id=role.tenant_id,
                    role_id=role.role_id,
                    revision_id=revision.revision_id,
                    version=1,
                )
            )
            await db_session.flush()


async def test_revision_role_cannot_belong_to_another_tenant(db_session, policy_rows):
    first, _second, roles, *_ = policy_rows
    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            db_session.add(
                UserFieldPolicyRevision(
                    tenant_id=first.tenant_id,
                    role_id=roles[2].role_id,
                    revision_no=1,
                    catalog_revision="user-fields/1",
                    catalog_digest=CATALOG_DIGEST,
                    rules=[],
                    rules_digest="a" * 64,
                    created_by_user_id=next_id(),
                )
            )
            await db_session.flush()


@pytest.mark.parametrize("history", ["revision", "audit"])
@pytest.mark.parametrize("mutation", ["update", "delete"])
async def test_history_cannot_be_updated_or_deleted(
    db_session, policy_rows, history, mutation
):
    *_, revision, _binding, audit = policy_rows
    if history == "revision":
        model = UserFieldPolicyRevision
        identity = model.revision_id == revision.revision_id
        values = {"rules_digest": "b" * 64}
    else:
        model = UserFieldPolicyAudit
        identity = model.audit_id == audit.audit_id
        values = {"reason": "Cannot rewrite history"}
    statement = (
        update(model).where(identity).values(**values)
        if mutation == "update"
        else delete(model).where(identity)
    )
    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            await db_session.execute(statement)


async def test_binding_archive_requires_version_and_keeps_history(
    db_session, policy_rows
):
    first, _second, roles, revision, _binding, _audit = policy_rows
    identity = (
        UserFieldPolicyBinding.tenant_id == first.tenant_id,
        UserFieldPolicyBinding.role_id == roles[0].role_id,
    )
    archived_at = datetime.now(UTC)
    for statement in (
        delete(UserFieldPolicyBinding).where(*identity),
        update(UserFieldPolicyBinding).where(*identity).values(version=0),
        update(UserFieldPolicyBinding).where(*identity).values(archived_at=archived_at),
    ):
        with pytest.raises(DBAPIError):
            async with db_session.begin_nested():
                await db_session.execute(statement)
    await db_session.execute(
        update(UserFieldPolicyBinding)
        .where(*identity)
        .values(archived_at=archived_at, version=2)
    )
    binding = await db_session.get(
        UserFieldPolicyBinding, (first.tenant_id, roles[0].role_id)
    )
    assert binding.version == 2
    assert binding.revision_id == revision.revision_id
    assert await db_session.get(UserFieldPolicyRevision, revision.revision_id)


@pytest.mark.parametrize("invalid_rules", [{}, [{}] * 257, [{"value": "x" * 65536}]])
async def test_revision_rules_have_database_shape_and_size_bounds(
    db_session, policy_rows, invalid_rules
):
    first, _second, roles, *_ = policy_rows
    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            db_session.add(
                UserFieldPolicyRevision(
                    tenant_id=first.tenant_id,
                    role_id=roles[0].role_id,
                    revision_no=2,
                    catalog_revision="user-fields/1",
                    catalog_digest=CATALOG_DIGEST,
                    rules=invalid_rules,
                    rules_digest="a" * 64,
                    created_by_user_id=next_id(),
                )
            )
            await db_session.flush()


@pytest.mark.parametrize("cross_tenant", [False, True])
async def test_audit_revision_cannot_cross_role_or_tenant(
    db_session, policy_rows, cross_tenant
):
    _first, _second, roles, revision, *_ = policy_rows
    role = roles[2] if cross_tenant else roles[1]
    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            db_session.add(
                UserFieldPolicyAudit(
                    tenant_id=role.tenant_id,
                    role_id=role.role_id,
                    actor_user_id=next_id(),
                    action="publish",
                    reason="Wrong revision target",
                    correlation_id="schema-invalid-audit",
                    after_digest="a" * 64,
                    revision_id=revision.revision_id,
                    before_version=0,
                    after_version=1,
                    epoch=2,
                )
            )
            await db_session.flush()


async def test_policy_history_blocks_role_delete_but_plain_role_remains_deletable(
    db_session, policy_rows
):
    _first, _second, roles, *_ = policy_rows
    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            await db_session.execute(
                delete(Role).where(Role.role_id == roles[0].role_id)
            )
    await db_session.execute(delete(Role).where(Role.role_id == roles[1].role_id))
    assert not await db_session.scalar(
        select(Role.role_id).where(Role.role_id == roles[1].role_id)
    )


async def test_tenant_delete_never_cascades_policy_state(db_session):
    tenant = await create_test_tenant(db_session, prefix="schema-keep")
    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            await db_session.execute(
                delete(Tenant).where(Tenant.tenant_id == tenant.tenant_id)
            )
