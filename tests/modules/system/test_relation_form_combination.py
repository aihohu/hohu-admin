"""Two-tenant HTTP composition with real owners and synthetic test qualification."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import insert, select, update

from app.core.config import settings
from app.core.field_policy import Grant
from app.core.field_policy.documents import catalog_parts, rules_document
from app.core.field_policy.qualification import Qualification
from app.core.security import create_access_token
from app.db.base import role_menus, user_roles
from app.modules.system.api import menu as menu_api
from app.modules.system.field_policy.ordinary_catalog import production_catalog
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from app.modules.system.service.ordinary_catalog_upgrade import prepare_catalog
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_system_http import (
    http_system as http_system,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_generic_policy_migration import fence
from tests.modules.system.test_generic_policy_persistence import run
from tests.modules.system.test_generic_policy_transitions import command


async def test_both_tenants_use_their_own_form_rules_and_epoch(
    http_system, system_sessions, monkeypatch
):
    client, plain = http_system
    client._transport.app.include_router(menu_api.router, prefix="/menu")
    catalog = production_catalog()
    monkeypatch.setattr(settings, "RELEASE_BUILD_SHA", "b" * 40)
    monkeypatch.setattr(settings, "TENANT_MODE", "hosted")
    monkeypatch.setattr(settings, "TENANT_HOSTED_LOGIN_ENABLED", True)
    async with plain() as db:
        await db.execute(
            update(Role).where(Role.role_id == 202).values(role_code="R_SUPER")
        )
        await db.execute(
            insert(user_roles).values(tenant_id=1, user_id=102, role_id=202)
        )
        await db.execute(
            insert(Menu).values(
                tenant_id=1,
                menu_id=20001,
                menu_name="Role edit",
                menu_type="F",
                permission="system:role:edit",
                status="1",
            )
        )
        await db.execute(
            insert(role_menus).values(tenant_id=1, role_id=202, menu_id=20001)
        )
        await db.execute(
            insert(Menu).values(
                tenant_id=1,
                menu_id=20002,
                menu_name="Dept edit",
                menu_type="F",
                permission="system:dept:edit",
                status="1",
            )
        )
        await db.execute(
            insert(Menu).values(
                tenant_id=1,
                menu_id=20003,
                menu_name="Dept list",
                menu_type="F",
                permission="system:dept:list",
                status="1",
            )
        )
        await db.execute(
            insert(role_menus),
            [
                {"tenant_id": 1, "role_id": 202, "menu_id": 20002},
                {"tenant_id": 1, "role_id": 202, "menu_id": 20003},
            ],
        )
        for tid in (0, 1):
            await db.execute(
                insert(Role).values(
                    tenant_id=tid,
                    role_id=303 + tid,
                    role_name=f"Business{tid}",
                    role_code="R_BUSINESS",
                    role_desc=f"Private{tid}",
                    status="1",
                    data_scope="1",
                )
            )
            await db.execute(
                insert(Dept).values(
                    tenant_id=tid,
                    dept_id=401 + tid,
                    dept_name=f"Department{tid}",
                    email=f"tenant{tid}@example.invalid",
                    status="1",
                )
            )
            await db.execute(
                insert(Menu).values(
                    tenant_id=tid,
                    menu_id=501 + tid,
                    menu_name=f"Menu{tid}",
                    menu_type="C",
                    status="1",
                )
            )
        await db.commit()
        await prepare_catalog(db, fence=await fence(db), source_build="b" * 40)
        await db.commit()
    proof = Qualification(
        "b" * 40,
        catalog.digest,
        *catalog_parts(catalog),
        "c" * 64,
        datetime.now(UTC) + timedelta(minutes=20),
    )
    policies = system_sessions[0], plain, catalog
    hidden = {
        ("system.role", "role_desc"),
        ("system.dept", "email"),
        ("system.menu", "menu_name"),
    }
    for tid in (0, 1):
        version = 1
        rid = str(201 + tid)
        for obj in catalog.objects:
            grants = tuple(
                Grant(tid, rid, ref, access)
                for ref, field in catalog.fields.items()
                if ref.object_id == obj
                for access in field.accesses
                if not (
                    tid == 1
                    and (obj, ref.field_id) in hidden
                    and access.operation == "read"
                )
            )
            await run(
                policies,
                command(
                    "publish",
                    version,
                    role_id=int(rid),
                    object_id=obj,
                    binding_version=0,
                    rules=rules_document(
                        catalog, grants, tenant_id=tid, role_id=int(rid), object_id=obj
                    ),
                ),
                tenant_id=tid,
            )
            version += 1
        built = await run(policies, command("build", version), tenant_id=tid)
        ready = await run(
            policies,
            command("ready", version + 1, expected_epoch=built["epoch"]),
            tenant_id=tid,
            qualification=proof,
        )
        await run(
            policies,
            command("activate", version + 2, expected_epoch=ready["epoch"]),
            tenant_id=tid,
            qualification=proof,
        )
    tokens = [
        create_access_token(
            subject=str(101 + tid), tenant_id=tid, tenant_version=1, user_version=1
        )
        for tid in (0, 1)
    ]
    for tid in (0, 1):
        headers = {"Authorization": f"Bearer {tokens[tid]}"}
        for surface, base, field in (
            ("role", 303, "roleDesc"),
            ("dept", 401, "email"),
            ("menu", 501, "menuName"),
        ):
            response = await client.get(
                f"/{surface}/form-context",
                params={"mode": "update", "recordId": str(base + tid)},
                headers=headers,
            )
            assert response.status_code == 200, response.text
            data = response.json()["data"]
            assert (field in data["record"]) == (tid == 0)
            assert data["fieldPolicy"]["readLevels"][
                {"roleDesc": "role_desc", "email": "email", "menuName": "menu_name"}[
                    field
                ]
            ] == ("plain" if tid == 0 else "hidden")
            foreign = await client.get(
                f"/{surface}/form-context",
                params={"mode": "update", "recordId": str(base + 1 - tid)},
                headers=headers,
            )
            assert foreign.status_code == 404, foreign.text
    headers = {"Authorization": f"Bearer {tokens[1]}"}
    changed = await client.put(
        "/role/304", json={"roleDesc": "Explicit blind replacement"}, headers=headers
    )
    assert changed.status_code == 200, changed.text
    rejected = await client.put(
        "/role/304",
        json={
            "roleName": "Old whole form",
            "roleCode": "R_BUSINESS",
            "roleDesc": "Bad",
        },
        headers=headers,
    )
    assert rejected.status_code == 422, rejected.text
    async with plain() as db:
        assert (await db.get(Role, 303)).role_desc == "Private0"
        assert (await db.get(Role, 304)).role_desc == "Explicit blind replacement"
        epoch = await db.scalar(
            select(UserFieldPolicyState.epoch).where(
                UserFieldPolicyState.tenant_id == 1
            )
        )
    await run(
        policies,
        command("freeze", version + 3, expected_epoch=epoch, object_id="system.role"),
        tenant_id=1,
    )
    blocked = await client.get(
        "/role/form-context",
        params={"mode": "update", "recordId": "304"},
        headers=headers,
    )
    assert blocked.status_code == 503, blocked.text
    unaffected = await client.get(
        "/role/form-context", params={"mode": "update", "recordId": "303"}
    )
    assert unaffected.status_code == 200, unaffected.text


async def test_tenant_super_role_cannot_use_read_bypass_as_a_write_permission(
    http_system, monkeypatch
):
    client, plain = http_system
    monkeypatch.setattr(settings, "TENANT_MODE", "hosted")
    monkeypatch.setattr(settings, "TENANT_HOSTED_LOGIN_ENABLED", True)
    async with plain() as db:
        await db.execute(
            update(Role).where(Role.role_id == 202).values(role_code="R_SUPER")
        )
        await db.execute(
            insert(user_roles).values(tenant_id=1, user_id=102, role_id=202)
        )
        await db.commit()
    token = create_access_token(
        subject="102", tenant_id=1, tenant_version=1, user_version=1
    )
    response = await client.get(
        "/role/form-context",
        params={"mode": "update", "recordId": "202"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403, response.text
