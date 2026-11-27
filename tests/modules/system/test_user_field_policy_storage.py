"""Candidate storage is tenant-scoped, atomic and never activates F1."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from app.core.exceptions import BusinessException
from app.core.id_generator import next_id
from app.core.tenant import TenantContext
from app.modules.system.field_policy.documents import catalog_digest, document_digest
from app.modules.system.field_policy.evaluator import FieldGrant
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import (
    UserFieldPolicyAudit,
    UserFieldPolicyBinding,
    UserFieldPolicyRevision,
    UserFieldPolicyState,
)
from app.modules.system.service.user_field_policy_service import (
    user_field_policy_service,
)


async def make_actor_role(db):
    suffix = str(next_id())
    actor = User(
        tenant_id=0, user_name="fp" + suffix, hashed_password="unused", status="1"
    )
    role = Role(
        tenant_id=0, role_name="fp" + suffix, role_code="fp" + suffix, status="1"
    )
    db.add_all([actor, role])
    await db.flush()
    tenant = TenantContext(0, "default", actor.user_id, 1, "access_token")
    return tenant, role.role_id


def grant(role_id):
    return FieldGrant(str(role_id), "nickname", "read", "management.read")


async def publish(db, tenant, role_id, expected_version=0, **kwargs):
    return await user_field_policy_service.publish(
        db,
        tenant=tenant,
        role_id=role_id,
        grants=(grant(role_id),),
        expected_version=expected_version,
        reason="reviewed candidate",
        correlation_id="test-candidate",
        **kwargs,
    )


async def test_publish_candidate_cas_archive_and_republish(db_session, monkeypatch):
    tenant, role_id = await make_actor_role(db_session)
    monkeypatch.setattr(
        db_session, "commit", AsyncMock(side_effect=AssertionError("service commit"))
    )
    first = await publish(db_session, tenant, role_id)
    assert first.version == 1 and first.role_id == str(role_id)
    assert first.rules == (grant(role_id),) and not first.archived
    assert first.created_at.utcoffset().total_seconds() == 0
    with pytest.raises(BusinessException) as error:
        await publish(db_session, tenant, role_id)
    assert error.value.error_code == "FIELD_POLICY_VERSION_CONFLICT"
    archived = await user_field_policy_service.archive(
        db_session,
        tenant=tenant,
        role_id=role_id,
        expected_version=1,
        reason="retired candidate",
        correlation_id="test-archive",
    )
    assert archived.version == 2 and archived.archived
    third = await publish(db_session, tenant, role_id, expected_version=2)
    assert third.version == 3 and third.revision_no == 3
    revisions = await db_session.scalar(
        select(func.count())
        .select_from(UserFieldPolicyRevision)
        .where(UserFieldPolicyRevision.role_id == role_id)
    )
    assert revisions == 2
    state = await user_field_policy_service.get_state(db_session, tenant=tenant)
    assert state.mode == "legacy" and not state.ever_enforced
    assert state.active_manifest_digest is None


async def test_failed_audit_rolls_back_revision_binding_and_epoch(
    db_session, monkeypatch
):
    tenant, role_id = await make_actor_role(db_session)
    before = await db_session.get(UserFieldPolicyState, tenant.tenant_id)
    epoch = before.epoch
    monkeypatch.setattr(
        user_field_policy_service,
        "_append_audit",
        AsyncMock(side_effect=RuntimeError("audit failed")),
    )
    with pytest.raises(RuntimeError, match="audit failed"):
        await publish(db_session, tenant, role_id)
    assert (
        await user_field_policy_service.get_policy(
            db_session, tenant=tenant, role_id=role_id
        )
        is None
    )
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(UserFieldPolicyRevision)
            .where(UserFieldPolicyRevision.role_id == role_id)
        )
        == 0
    )
    assert (
        await db_session.scalar(
            select(UserFieldPolicyState.epoch).where(
                UserFieldPolicyState.tenant_id == tenant.tenant_id
            )
        )
        == epoch
    )


async def test_ready_requires_current_manifest_and_epoch_and_cannot_activate(
    db_session,
):
    tenant, role_id = await make_actor_role(db_session)
    await publish(db_session, tenant, role_id)
    state = await user_field_policy_service.get_state(db_session, tenant=tenant)
    with pytest.raises(BusinessException) as error:
        await user_field_policy_service.mark_ready(
            db_session,
            tenant=tenant,
            expected_epoch=state.epoch,
            expected_manifest_digest="0" * 64,
            reason="preview",
            correlation_id="ready",
        )
    assert error.value.error_code == "FIELD_POLICY_STALE"
    ready = await user_field_policy_service.mark_ready(
        db_session,
        tenant=tenant,
        expected_epoch=state.epoch,
        expected_manifest_digest=state.candidate_manifest_digest,
        reason="preview",
        correlation_id="ready",
    )
    assert ready.mode == "ready" and ready.epoch == state.epoch + 1
    assert not ready.ever_enforced and ready.active_manifest_digest is None
    await publish(db_session, tenant, role_id, expected_version=1)
    changed = await user_field_policy_service.get_state(db_session, tenant=tenant)
    assert changed.mode == "legacy"  # changed candidates require a new preview


@pytest.mark.parametrize("missing", ["actor", "role"])
async def test_wrong_tenant_actor_or_missing_role_is_invisible(db_session, missing):
    tenant, role_id = await make_actor_role(db_session)
    forged_actor = TenantContext(0, "default", next_id(), 1, "access_token")
    context, target = (
        (forged_actor, role_id) if missing == "actor" else (tenant, next_id())
    )
    with pytest.raises(BusinessException) as error:
        await publish(db_session, context, target)
    assert error.value.code == 404
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(UserFieldPolicyAudit)
            .where(UserFieldPolicyAudit.role_id == role_id)
        )
        == 0
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"source_digest": "f" * 64},
        {"template_revision": "legacy-user-fields/v1"},
        {"source_digest": "bad", "template_revision": "legacy-user-fields/v1"},
    ],
)
async def test_candidate_source_metadata_requires_valid_pair(db_session, kwargs):
    tenant, role_id = await make_actor_role(db_session)
    with pytest.raises(BusinessException) as error:
        await publish(db_session, tenant, role_id, **kwargs)
    assert error.value.error_code == "FIELD_POLICY_INVALID"


@pytest.mark.parametrize("operation", ["state", "ready"])
@pytest.mark.parametrize("corruption", ["unsupported_rule", "wrong_digest"])
async def test_invalid_rule_content_cannot_be_accepted_by_manifest(
    db_session, operation, corruption
):
    tenant, role_id = await make_actor_role(db_session)
    rules = [
        {
            "field_id": "password" if corruption == "unsupported_rule" else "nickname",
            "operation": "read",
            "purpose": "management.read",
            "effect": "allow",
            "read": "plain",
            "mask_id": None,
        }
    ]
    revision = UserFieldPolicyRevision(
        tenant_id=0,
        role_id=role_id,
        revision_no=1,
        catalog_revision="user-fields/1",
        catalog_digest=catalog_digest(),
        rules=rules,
        rules_digest=(
            document_digest(rules) if corruption == "unsupported_rule" else "0" * 64
        ),
        created_by_user_id=tenant.actor_user_id,
    )
    db_session.add(revision)
    await db_session.flush()
    db_session.add(
        UserFieldPolicyBinding(
            tenant_id=0, role_id=role_id, revision_id=revision.revision_id, version=1
        )
    )
    state = await db_session.get(UserFieldPolicyState, 0)
    digest = document_digest(
        {
            "catalog_digest": catalog_digest(),
            "bindings": [
                {
                    "role_id": str(role_id),
                    "revision_id": str(revision.revision_id),
                    "version": 1,
                    "archived": False,
                    "rules_digest": revision.rules_digest,
                }
            ],
        }
    )
    state.epoch += 1
    state.candidate_manifest_digest = digest
    await db_session.flush()
    with pytest.raises(BusinessException) as error:
        if operation == "state":
            await user_field_policy_service.get_state(db_session, tenant=tenant)
        else:
            await user_field_policy_service.mark_ready(
                db_session,
                tenant=tenant,
                expected_epoch=state.epoch,
                expected_manifest_digest=digest,
                reason="preview",
                correlation_id="invalid-document",
            )
    assert error.value.error_code == "FIELD_POLICY_UNAVAILABLE"
