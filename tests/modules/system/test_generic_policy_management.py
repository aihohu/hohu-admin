"""Management preview authority and candidate writes use real protected transactions."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import delete, insert, select, update

from app.core.exceptions import BusinessException
from app.db.authorization_field_management_owner import FieldManagementOwner
from app.db.base import role_menus
from app.modules.system.models.field_policy import FieldPolicyHead, FieldPolicyRevision
from app.modules.system.models.menu import Menu
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from tests.modules.system.test_generic_policy_owner import (  # noqa: F401
    admin_sessions,
    generic_sessions,
    plan_sessions,
    policies,
    policy_sessions,
)
from tests.modules.system.test_generic_policy_owner import (
    managed as managed,
)
from tests.modules.system.test_planned_user_field_policy import TENANT
from tests.modules.system.test_user_field_policy_admin_owner import grant
from tools.ops.policy_preparation_identity import require_preparation_administrators


async def call(
    managed, operation, payload=None, *, role_id=201, object_id="sample.item"
):
    factory, _, catalog = managed
    async with factory() as db:
        return await FieldManagementOwner().execute(
            db,
            tenant=TENANT,
            actor_user_version=1,
            catalog=catalog,
            operation=operation,
            payload=payload or {},
            role_id=role_id,
            object_id=object_id,
        )


async def permissions(managed):
    await grant(managed[1], "system:role:field-policy-view")
    async with managed[1]() as db:
        await db.execute(
            insert(Menu).values(
                menu_id=302,
                tenant_id=0,
                menu_name="edit policy",
                status="1",
                permission="system:role:field-policy-edit",
            )
        )
        await db.execute(
            insert(role_menus).values(tenant_id=0, role_id=201, menu_id=302)
        )
        await db.commit()


def draft(document):
    return {
        "catalogDigest": document["catalogDigest"],
        "expectedVersion": document["headVersion"],
        "expectedBindingVersion": document["bindingVersion"],
        "rules": [
            {
                "fieldId": "name",
                "access": {
                    "operation": "read",
                    "purpose": "management.read",
                    "use": "",
                },
                "effect": "allow",
                "level": "plain",
                "maskId": None,
            }
        ],
    }


async def test_read_requires_current_dedicated_authority_and_never_crosses_tenant(
    managed,
):
    with pytest.raises(BusinessException) as denied:
        await call(managed, "object")
    assert denied.value.error_code == "FIELD_POLICY_MANAGEMENT_FORBIDDEN"
    await grant(managed[1], "system:role:field-policy-view")
    value = await call(managed, "object")
    assert value["bindingVersion"] == "0"
    assert value["headVersion"] == "1"
    assert value["rules"] == []
    assert value["capabilities"]["edit"] is False
    for role_id in (202, 987654):
        with pytest.raises(BusinessException) as rejected:
            await call(managed, "object", role_id=role_id)
        assert rejected.value.error_code == "FIELD_POLICY_MANAGEMENT_FORBIDDEN"


async def test_preview_save_round_trip_separates_active_and_preserves_receipt(managed):
    await permissions(managed)
    document = await call(managed, "object")
    payload = draft(document)
    preview = await call(managed, "preview", payload)
    assert preview["safeSamples"] == []
    assert preview["diff"][0]["fieldId"] == "name"
    save = payload | {
        "previewDigest": preview["previewDigest"],
        "reason": "candidate only",
        "requestId": str(uuid4()),
    }
    result = await call(managed, "publish", save)
    assert result["headVersion"] == "2"
    assert await call(managed, "publish", save) == result
    assert (await call(managed, "object"))["bindingVersion"] == "1"
    assert await call(managed, "receipt", {"requestId": save["requestId"]}) == result
    async with managed[1]() as db:
        head = await db.get(FieldPolicyHead, 0)
        assert head.active_manifest_id is None
        assert len((await db.scalars(select(FieldPolicyRevision))).all()) == 1


async def test_preview_rejects_changed_rules_or_epoch_without_writing(managed):
    await permissions(managed)
    payload = draft(await call(managed, "object"))
    preview = await call(managed, "preview", payload)
    save = payload | {
        "previewDigest": preview["previewDigest"],
        "reason": "candidate",
        "requestId": str(uuid4()),
    }
    with pytest.raises(BusinessException) as stale:
        await call(managed, "publish", save | {"rules": []})
    assert stale.value.error_code == "FIELD_POLICY_PREVIEW_STALE"
    async with managed[1]() as db:
        await db.execute(
            update(UserFieldPolicyState)
            .where(UserFieldPolicyState.tenant_id == 0)
            .values(epoch=2)
        )
        await db.commit()
    with pytest.raises(BusinessException) as stale:
        await call(managed, "publish", save)
    assert stale.value.error_code == "FIELD_POLICY_PREVIEW_STALE"
    async with managed[1]() as db:
        assert not (await db.scalars(select(FieldPolicyRevision))).all()


@pytest.mark.parametrize(
    "change",
    [{"tenantId": "1"}, {"expectedVersion": 1}, {"expectedBindingVersion": "00"}],
)
async def test_closed_dto_rejects_authority_injection_and_lossy_versions(
    managed, change
):
    await permissions(managed)
    payload = draft(await call(managed, "object")) | change
    with pytest.raises(BusinessException) as invalid:
        await call(managed, "preview", payload)
    assert invalid.value.code == 422


async def test_manifest_preview_cannot_activate_compatibility_catalog(managed):
    await permissions(managed)
    current = await call(managed, "manifest")
    assert current["canActivate"] is False
    payload = {
        "expectedVersion": current["headVersion"],
        "catalogDigest": current["catalogDigest"],
    }
    preview = await call(managed, "manifest-preview", payload)
    saved = await call(
        managed,
        "build",
        payload
        | {
            "previewDigest": preview["previewDigest"],
            "reason": "seal complete candidate",
            "requestId": str(uuid4()),
        },
    )
    assert saved["headVersion"] == "2"
    with pytest.raises(BusinessException) as blocked:
        await call(
            managed,
            "ready",
            {
                "expectedVersion": "2",
                "expectedEpoch": "2",
                "reason": "not qualified",
                "requestId": str(uuid4()),
            },
        )
    assert blocked.value.error_code == "FIELD_POLICY_ACTIVATION_BLOCKED"


async def test_concurrent_candidate_publish_has_one_winner_and_one_conflict(managed):
    await permissions(managed)
    payload = draft(await call(managed, "object"))
    preview = await call(managed, "preview", payload)
    save = payload | {"previewDigest": preview["previewDigest"], "reason": "concurrent"}
    results = await asyncio.gather(
        *(
            call(managed, "publish", save | {"requestId": str(uuid4())})
            for _ in range(2)
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    conflict = next(r for r in results if isinstance(r, BusinessException))
    assert conflict.code == 409
    async with managed[1]() as db:
        assert len((await db.scalars(select(FieldPolicyRevision))).all()) == 1
        assert (await db.get(FieldPolicyHead, 0)).active_manifest_id is None


async def test_revoked_permission_cannot_use_preview_or_existing_receipt(managed):
    await permissions(managed)
    payload = draft(await call(managed, "object"))
    preview = await call(managed, "preview", payload)
    save = payload | {
        "previewDigest": preview["previewDigest"],
        "reason": "revoke",
        "requestId": str(uuid4()),
    }
    await call(managed, "publish", save)
    async with managed[1]() as db:
        await db.execute(
            delete(role_menus).where(
                role_menus.c.tenant_id == 0, role_menus.c.menu_id == 302
            )
        )
        await db.commit()
    with pytest.raises(BusinessException) as denied:
        await call(managed, "publish", save)
    assert denied.value.error_code == "FIELD_POLICY_MANAGEMENT_FORBIDDEN"
    assert await call(managed, "receipt", {"requestId": str(uuid4())}) is None


async def test_preparation_preflight_requires_all_permissions_for_each_enabled_tenant(
    managed,
):
    await permissions(managed)
    with pytest.raises(BusinessException) as rejected:
        await require_preparation_administrators(managed[1])
    assert rejected.value.error_code == "FIELD_POLICY_ADMIN_PREFLIGHT_REQUIRED"
