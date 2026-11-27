"""Product contract for scoped queries, publish-only actions and old XLSX files."""

from dataclasses import replace
from io import BytesIO

import pytest
from openpyxl import load_workbook
from sqlalchemy import delete, insert, select, update

from app.core.config import settings
from app.core.field_policy import FieldRef, Grant
from app.core.field_policy.documents import rules_document
from app.core.file_storage import LocalFileStorage
from app.core.security import create_access_token
from app.db import authorization_runtime
from app.db.base import role_menus, user_depts, user_roles
from app.modules.system.field_policy.ordinary_catalog import production_catalog
from app.modules.system.models.config import Config
from app.modules.system.models.config_publication import ConfigPublication
from app.modules.system.models.dept import Dept
from app.modules.system.models.field_policy import FieldPolicyHead
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.models.user_transfer import UserExportTask
from app.modules.system.service import user_export_service
from tests.modules.system.test_generic_policy_persistence import run as policy_command
from tests.modules.system.test_generic_policy_transitions import command
from tests.modules.system.test_parameter_http import (  # noqa: F401
    http_system,
    plan_sessions,
    system_sessions,
)
from tests.modules.system.test_parameter_http import (
    parameter_http as parameter_http,
)
from tests.modules.system.test_parameter_read_owner import parameters as parameters
from tests.modules.system.test_relation_field_writes import (
    activate_candidates,
    activate_relations,
)


def token(tenant):
    jwt = create_access_token(
        subject=str(101 + tenant), tenant_id=tenant, tenant_version=1, user_version=1
    )
    return {"Authorization": f"Bearer {jwt}"}


@pytest.fixture(params=[0, 1], ids=["default-tenant", "hosted-tenant"])
async def tenant_id(request, parameter_http, monkeypatch, tmp_path):
    tenant = request.param
    client, plain = parameter_http
    monkeypatch.setattr(settings, "TENANT_MODE", "hosted")
    monkeypatch.setattr(settings, "TENANT_HOSTED_LOGIN_ENABLED", True)
    storage = LocalFileStorage(tmp_path)
    monkeypatch.setattr(authorization_runtime, "get_file_storage", lambda: storage)
    monkeypatch.setattr(user_export_service, "get_file_storage", lambda: storage)
    client.headers.update(token(tenant))
    async with plain() as db:
        # Both subjects are ordinary, active operators with the same functional
        # permissions. Keep the other tenant's subject in the same DB.
        for tid in (0, 1):
            role = 201 + tid
            await db.execute(
                update(Role).where(Role.role_id == role).values(role_code="OPERATOR")
            )
            if tid == 1:
                await db.execute(
                    insert(user_roles).values(tenant_id=tid, user_id=102, role_id=role)
                )
            for i, perm in enumerate(
                (
                    "system:user:list",
                    "system:user:export",
                    "system:config:list",
                    "system:config:edit",
                    "system:config:publish",
                )
            ):
                mid = await db.scalar(
                    select(Menu.menu_id).where(
                        Menu.tenant_id == tid, Menu.permission == perm
                    )
                )
                if mid is None:
                    mid = 9000 + tid * 100 + i
                    await db.execute(
                        insert(Menu).values(
                            tenant_id=tid,
                            menu_id=mid,
                            menu_name=perm,
                            menu_type="F",
                            permission=perm,
                            status="1",
                        )
                    )
                bound = await db.scalar(
                    select(role_menus.c.menu_id).where(
                        role_menus.c.role_id == role, role_menus.c.menu_id == mid
                    )
                )
                if bound is None:
                    await db.execute(
                        insert(role_menus).values(
                            tenant_id=tid, role_id=role, menu_id=mid
                        )
                    )
            await db.execute(
                update(User)
                .where(User.user_id == 101 + tid)
                .values(user_phone=f"1390000000{tid}")
            )
        await db.commit()
    return tenant


def grants_for(tenant, role, obj):
    catalog = production_catalog()
    return [
        Grant(tenant, str(role), ref, access)
        for ref, field in catalog.fields.items()
        if ref.object_id == obj
        for access in field.accesses
    ]


@pytest.mark.parametrize(
    "department_only", [False, True], ids=["whole-tenant", "own-dept"]
)
async def test_phone_sort_requires_coverage_of_requested_scope(
    tenant_id, parameters, parameter_http, monkeypatch, department_only
):
    tenant = tenant_id
    client, plain = parameter_http
    actor, role, local = 101 + tenant, 820, 701
    async with plain() as db:
        for did in (local, local + 1):
            await db.execute(
                insert(Dept).values(
                    tenant_id=tenant,
                    dept_id=did,
                    dept_name=str(did),
                    ancestors="0",
                    status="1",
                )
            )
        for uid, phone in [(811, "13800000001"), (812, "13700000002")]:
            await db.execute(
                insert(User).values(
                    tenant_id=tenant,
                    user_id=uid,
                    user_name=f"customer{uid}",
                    hashed_password="fixture",
                    status="1",
                    user_phone=phone,
                )
            )
        for uid, did in [(actor, local), (811, local), (812, local + 1)]:
            await db.execute(
                insert(user_depts).values(
                    tenant_id=tenant, user_id=uid, dept_id=did, is_primary="Y"
                )
            )
        await db.execute(
            insert(Role).values(
                tenant_id=tenant,
                role_id=role,
                role_code="DEPT_PHONE",
                role_name="Department phone",
                data_scope="3",
                status="1",
            )
        )
        await db.execute(
            insert(user_roles).values(tenant_id=tenant, user_id=actor, role_id=role)
        )
        list_menu = await db.scalar(
            select(Menu.menu_id).where(
                Menu.tenant_id == tenant, Menu.permission == "system:user:list"
            )
        )
        await db.execute(
            insert(role_menus).values(tenant_id=tenant, role_id=role, menu_id=list_menu)
        )
        await db.commit()
    obj = "system.user"
    all_grants = grants_for(tenant, role, obj)
    phone_queries = [
        g
        for g in all_grants
        if g.ref.field_id == "user_phone" and g.access.purpose == "management.query"
    ]
    await activate_relations(
        parameters,
        monkeypatch,
        tenant_id=tenant,
        missing={(obj, "user_phone", g.access.operation) for g in phone_queries},
        extra_grants=tuple(all_grants),
    )
    unrestricted = await client.get("/user/list")
    assert unrestricted.status_code == 200, unrestricted.text
    assert {r["userId"] for r in unrestricted.json()["data"]["records"]} == {
        str(actor),
        "811",
        "812",
    }
    query = {"sortBy": "userPhone", "sortOrder": "asc"}
    if department_only:
        query["deptId"] = str(local)
    response = await client.get("/user/list", params=query)
    if department_only:
        assert response.status_code == 200, response.text
        page = response.json()["data"]
        assert (page["total"], [r["userId"] for r in page["records"]]) == (
            2,
            ["811", str(actor)],
        )
        reverse = await client.get("/user/list", params=query | {"sortOrder": "desc"})
        assert reverse.status_code == 200
        assert [r["userId"] for r in reverse.json()["data"]["records"]] == [
            str(actor),
            "811",
        ]
    else:
        assert response.status_code == 403
        assert response.json()["data"] is None


@pytest.mark.parametrize(
    "scenario", ["unchanged-value", "undeclared-edit", "stale-confirmation"]
)
async def test_config_publish_is_separate_from_writing_the_content(
    tenant_id, parameters, parameter_http, monkeypatch, scenario
):
    client, plain = parameter_http
    target = 301 if tenant_id == 0 else 304
    form_params = {"mode": "update", "recordId": str(target)}
    await activate_relations(
        parameters,
        monkeypatch,
        tenant_id=tenant_id,
        missing=(("system.config", "config_value", "update"),),
    )
    async with plain() as db:
        await db.execute(
            delete(role_menus).where(
                role_menus.c.tenant_id == tenant_id,
                role_menus.c.role_id == 201 + tenant_id,
                role_menus.c.menu_id.in_(
                    select(Menu.menu_id).where(Menu.permission == "system:config:edit")
                ),
            )
        )
        await db.commit()
    form = await client.get("/config/form", params=form_params)
    assert form.status_code == 200, form.text
    fields = form.json()["data"]
    assert fields["record"]["configValue"] == f"value-{target}"
    assert fields["fieldPolicy"]["update"] == []
    revision = fields.get("publicationRevision")
    assert revision, "Publisher cannot confirm the unchanged value"
    payload = {"confirmed": True, "expectedRevision": revision}
    expected_value = f"value-{target}"
    if scenario == "undeclared-edit":
        payload["configValue"] = "edited"
    elif scenario == "stale-confirmation":
        expected_value = "changed since confirmation"
        async with plain() as db:
            await db.execute(
                update(Config)
                .where(Config.tenant_id == tenant_id, Config.config_id == target)
                .values(config_value=expected_value)
            )
            await db.commit()
    response = await client.put(f"/config/{target}/publication", json=payload)
    async with plain() as db:
        assert (
            await db.scalar(
                select(Config.config_value).where(Config.config_id == target)
            )
            == expected_value
        )
        published = list(await db.scalars(select(ConfigPublication.config_id)))
    if scenario == "stale-confirmation":
        assert response.status_code == 409, response.text
        assert published == []
    elif scenario == "undeclared-edit":
        assert response.status_code in (403, 422), response.text
        error = response.json()
        if response.status_code == 422:
            assert [item["field"] for item in error["data"]["fieldErrors"]] == [
                "configValue"
            ]
        else:
            assert error["errorCode"] == "FIELD_POLICY_DENIED"
        assert published == []
    else:
        assert response.status_code == 200, response.text
        assert published == [target]


@pytest.mark.parametrize(
    "change", ["unrelated-change", "plain-to-masked", "query-revoked"]
)
async def test_old_xlsx_checked_against_actual_content(
    tenant_id, parameters, parameter_http, monkeypatch, change
):
    client, plain = parameter_http
    tenant = tenant_id
    await activate_relations(parameters, monkeypatch, tenant_id=tenant)
    path = "/user/export"
    exported = await client.post(path, json={"reason": "field contract", "status": "1"})
    assert exported.status_code == 200, exported.text[:160]
    phone = f"1390000000{tenant}"

    def workbook_phones(data):
        book = load_workbook(BytesIO(data), read_only=True)
        try:
            rows = iter(book.active.values)
            col = next(rows).index("手机号")
            return [r[col] for r in rows]
        finally:
            book.close()

    assert workbook_phones(exported.content) == [phone]
    async with plain() as db:
        identifier = await db.scalar(
            select(UserExportTask.export_id).where(UserExportTask.tenant_id == tenant)
        )
        version = (await db.get(FieldPolicyHead, tenant)).head_version
    url = f"{path}/{identifier}/download"
    before = await client.get(url)
    assert before.status_code == 200 and before.content == exported.content
    foreign = await client.get(url, headers=token(1 - tenant))
    assert foreign.status_code in (403, 404), foreign.status_code
    obj = "system.config" if change == "unrelated-change" else "system.user"
    role = 201 + tenant
    rules = grants_for(tenant, role, obj)
    if change == "plain-to-masked":
        rules = [
            replace(g, level="masked", mask_id="phone/v1")
            if g.ref == FieldRef(obj, "user_phone") and g.access.operation == "export"
            else g
            for g in rules
        ]
    elif change == "query-revoked":
        rules = [
            g
            for g in rules
            if not (
                g.ref.field_id == "status" and g.access.purpose == "management.query"
            )
        ]
    else:
        rules = [g for g in rules if g.ref.field_id != "remark"]
    catalog = production_catalog()
    policies = (*parameters, catalog)
    await policy_command(
        policies,
        command(
            "publish",
            version,
            role_id=role,
            object_id=obj,
            binding_version=1,
            rules=rules_document(
                catalog, tuple(rules), tenant_id=tenant, role_id=role, object_id=obj
            ),
        ),
        tenant_id=tenant,
    )
    await activate_candidates(policies, version + 1, tenant_id=tenant)
    after = await client.get(url)
    if change == "query-revoked":
        assert after.status_code == 403, after.text[:160]
        assert after.json()["errorCode"] == "FIELD_QUERY_DENIED"
    elif change == "plain-to-masked":
        assert after.status_code == 403, after.status_code
        assert after.json()["errorCode"] == "FIELD_TRANSFER_REGENERATE_REQUIRED"
        fresh = await client.post(path, json={"reason": "regenerate masked"})
        assert fresh.status_code == 200, fresh.status_code
        assert workbook_phones(fresh.content) == [phone[:3] + "****" + phone[-4:]]
    else:
        assert after.status_code == 200, after.text[:160]
        assert after.content == exported.content
