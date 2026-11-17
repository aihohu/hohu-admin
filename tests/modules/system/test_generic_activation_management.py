"""Production management transitions bind current authority, qualification and target."""

import json
from uuid import uuid4

import pytest
from sqlalchemy import delete, insert, select

from app.core.config import settings
from app.core.exceptions import BusinessException
from app.db.authorization_field_management_owner import FieldManagementOwner
from app.db.base import role_menus
from app.modules.system.models.field_policy import (
    FieldPolicyHead,
    FieldPolicyObjectState,
)
from app.modules.system.models.menu import Menu
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from tests.core.test_generic_activation_configuration import qualification_document
from tests.modules.system.test_generic_policy_management import (  # noqa: F401
    admin_sessions,
    generic_sessions,
    permissions,
    plan_sessions,
    policies,
    policy_sessions,
)
from tests.modules.system.test_generic_policy_management import managed as managed
from tests.modules.system.test_generic_policy_persistence import run
from tests.modules.system.test_planned_user_field_policy import TENANT


async def setup_candidate(managed, tmp_path, monkeypatch):
    await permissions(managed)
    async with managed[1]() as db:
        await db.execute(
            insert(Menu).values(
                tenant_id=0,
                menu_id=303,
                menu_name="activate",
                status="1",
                permission="system:role:field-policy-activate",
            )
        )
        await db.execute(
            insert(role_menus).values(tenant_id=0, role_id=201, menu_id=303)
        )
        await db.commit()
    path = tmp_path / "synthetic-unit-qualification.json"
    path.write_text(json.dumps(qualification_document(managed[2])), encoding="utf-8")
    monkeypatch.setattr(settings, "RELEASE_BUILD_SHA", "b" * 40)
    monkeypatch.setattr(settings, "FIELD_POLICY_QUALIFICATION_FILE", str(path))
    await run(
        managed,
        {
            "action": "build",
            "request_id": str(uuid4()),
            "expected_version": 1,
            "reason": "sealed fixture",
        },
    )
    return path


async def target(managed):
    async with managed[1]() as db:
        head = await db.get(FieldPolicyHead, 0)
        barrier = await db.get(UserFieldPolicyState, 0)
        return head.candidate_manifest_id, {
            "catalogDigest": managed[2].digest,
            "expectedVersion": str(head.head_version),
            "expectedEpoch": str(barrier.epoch),
            "candidateManifestDigest": head.candidate_manifest_digest,
            "requestId": str(uuid4()),
            "reason": "qualified fixture transition",
        }


async def transition(managed, operation, manifest_id, payload):
    async with managed[0]() as db:
        return await FieldManagementOwner().execute(
            db,
            tenant=TENANT,
            actor_user_version=1,
            catalog=managed[2],
            operation=operation,
            payload=payload,
            manifest_id=manifest_id,
        )


async def test_qualified_ready_activate_and_replay_commit_one_atomic_manifest(
    managed, tmp_path, monkeypatch
):
    await setup_candidate(managed, tmp_path, monkeypatch)
    manifest_id, payload = await target(managed)
    ready = await transition(managed, "ready", manifest_id, payload)
    assert ready["headVersion"] == "3"
    assert await transition(managed, "ready", manifest_id, payload) == ready
    manifest_id, payload = await target(managed)
    active = await transition(managed, "activate", manifest_id, payload)
    assert active["headVersion"] == "4"
    assert await transition(managed, "activate", manifest_id, payload) == active
    async with managed[1]() as db:
        head = await db.get(FieldPolicyHead, 0)
        assert (
            head.active_manifest_id == manifest_id and head.rollout_state == "enforcing"
        )
        objects = (
            await db.scalars(
                select(FieldPolicyObjectState).where(
                    FieldPolicyObjectState.tenant_id == 0
                )
            )
        ).all()
        assert objects and all(
            row.mode == "enforcing" and row.ever_enforced for row in objects
        )
        assert (await db.get(FieldPolicyHead, 1)).active_manifest_id is None


@pytest.mark.parametrize(
    "fault", ["path", "digest", "epoch", "qualification", "revoked"]
)
async def test_invalid_activation_never_moves_active_head(
    managed, tmp_path, monkeypatch, fault
):
    path = await setup_candidate(managed, tmp_path, monkeypatch)
    manifest_id, payload = await target(managed)
    await transition(managed, "ready", manifest_id, payload)
    manifest_id, payload = await target(managed)
    if fault == "path":
        manifest_id += 1
    if fault == "digest":
        payload["candidateManifestDigest"] = "c" * 64
    if fault == "epoch":
        payload["expectedEpoch"] = "1"
    if fault == "qualification":
        path.write_text("{}", encoding="utf-8")
    if fault == "revoked":
        async with managed[1]() as db:
            await db.execute(
                delete(role_menus).where(
                    role_menus.c.tenant_id == 0, role_menus.c.menu_id == 303
                )
            )
            await db.commit()
    with pytest.raises(BusinessException) as denied:
        await transition(managed, "activate", manifest_id, payload)
    assert denied.value.code in {403, 409}
    async with managed[1]() as db:
        assert (await db.get(FieldPolicyHead, 0)).active_manifest_id is None
