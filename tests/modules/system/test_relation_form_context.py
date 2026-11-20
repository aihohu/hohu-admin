"""The edit snapshot and capability hints share one current read barrier."""

import pytest
from sqlalchemy import event, insert, select, update

from app.core.field_policy import FieldRef, Grant
from app.db.base import user_roles
from app.modules.system.api import menu as menu_api
from app.modules.system.field_policy.relation_catalog import READ
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_system_http import (
    http_system as http_system,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_relation_field_writes import activate_relations


async def seed_role(plain):
    async with plain() as db:
        await db.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=302,
                role_name="Editable",
                role_code="R_FORM",
                role_desc="Loaded from detail",
                data_scope="1",
                status="1",
            )
        )
        await db.commit()


async def test_update_context_loads_detail_and_keeps_code_immutable(http_system):
    client, plain = http_system
    await seed_role(plain)
    response = await client.get(
        "/role/form-context", params={"mode": "update", "recordId": "302"}
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["record"]["roleId"] == "302"
    assert data["record"]["roleDesc"] == "Loaded from detail"
    assert "role_name" in data["fieldPolicy"]["update"]
    assert (
        "role_code"
        not in data["fieldPolicy"]["update"] + data["fieldPolicy"]["specialUpdate"]
    )


async def test_write_only_field_is_not_filled_from_a_hidden_snapshot(
    http_system, system_sessions, monkeypatch
):
    client, plain = http_system
    await seed_role(plain)
    await activate_relations(
        system_sessions, monkeypatch, missing={("system.role", "role_name", "read")}
    )
    response = await client.get(
        "/role/form-context", params={"mode": "update", "recordId": "302"}
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert "roleName" not in data["record"]
    assert "i18nKeys" not in data["record"]
    assert "role_name" in data["fieldPolicy"]["update"]


async def test_create_context_is_rollback_only_and_contains_no_business_record(
    http_system,
):
    client, plain = http_system
    async with plain() as db:
        before = await db.scalar(
            select(UserFieldPolicyState.epoch).where(
                UserFieldPolicyState.tenant_id == 0
            )
        )
    commits = []

    def committed(connection):
        commits.append(connection)

    event.listen(plain.kw["bind"].sync_engine, "commit", committed)
    try:
        response = await client.get("/role/form-context", params={"mode": "create"})
    finally:
        event.remove(plain.kw["bind"].sync_engine, "commit", committed)
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data.get("record") is None
    assert data["fieldPolicy"]["canCreate"] is True
    assert "role_code" in data["fieldPolicy"]["create"]
    assert commits == []
    async with plain() as db:
        assert (
            await db.scalar(
                select(UserFieldPolicyState.epoch).where(
                    UserFieldPolicyState.tenant_id == 0
                )
            )
            == before
        )


async def test_form_context_cannot_read_a_foreign_tenant_target(http_system):
    client, _ = http_system
    response = await client.get(
        "/role/form-context", params={"mode": "update", "recordId": "202"}
    )
    assert response.status_code == 404, response.text


async def test_hidden_menu_button_is_not_returned_as_a_partial_replacement(
    http_system, system_sessions, monkeypatch
):
    client, plain = http_system
    client._transport.app.include_router(menu_api.router, prefix="/menu")
    async with plain() as db:
        await db.execute(
            insert(Menu).values(
                tenant_id=0, menu_id=3001, menu_name="Page", menu_type="C", status="1"
            )
        )
        await db.execute(
            insert(Menu).values(
                tenant_id=0,
                menu_id=3002,
                parent_id=3001,
                menu_name="List",
                menu_type="F",
                status="1",
                permission="system:user:list",
            )
        )
        await db.commit()
    await activate_relations(
        system_sessions, monkeypatch, missing={("system.menu", "permission", "read")}
    )
    response = await client.get(
        "/menu/form-context", params={"mode": "update", "recordId": "3001"}
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert "buttons" not in data["record"]
    assert data["fieldPolicy"]["complete"]["buttons"] is False
    assert "buttons" not in data["fieldPolicy"]["specialUpdate"]


async def test_member_pages_carry_the_current_barrier_epoch(http_system):
    client, plain = http_system
    async with plain() as db:
        await db.execute(
            insert(Dept).values(
                tenant_id=0, dept_id=401, dept_name="Members", status="1"
            )
        )
        await db.commit()
    first = await client.get("/dept/401/users", params={"current": 1, "size": 100})
    assert first.status_code == 200, first.text
    assert first.json()["data"]["authorizationEpoch"] == "1"
    changed = await client.put("/user/profile", json={"nickname": "New epoch"})
    assert changed.status_code == 200, changed.text
    second = await client.get("/dept/401/users", params={"current": 1, "size": 100})
    assert second.json()["data"]["authorizationEpoch"] == "2"


async def test_list_capabilities_do_not_infer_query_or_write_from_read(
    http_system, system_sessions, monkeypatch
):
    client, plain = http_system
    await seed_role(plain)
    await activate_relations(
        system_sessions,
        monkeypatch,
        missing={
            ("system.role", "role_name", "read"),
            ("system.role", "role_name", "contains"),
            ("system.role", "role_desc", "create"),
        },
    )
    response = await client.get("/role/list")
    assert response.status_code == 200, response.text
    page = response.json()["data"]
    assert "role_name" not in page["fieldPolicy"]["query"]
    assert "role_name" not in page["fieldPolicy"]["read"]
    assert page["fieldPolicy"]["canCreate"] is False
    target = next(r for r in page["records"] if r["roleId"] == "302")
    assert "roleDesc" not in target
    assert "role_name" in target["fieldPolicy"]["update"]


async def test_leader_query_hint_requires_current_user_source_query(
    http_system, system_sessions, monkeypatch
):
    client, plain = http_system
    async with plain() as db:
        await db.execute(
            insert(Dept).values(
                tenant_id=0,
                dept_id=401,
                dept_name="Contact",
                status="1",
                leader="planner",
                leader_user_id=101,
            )
        )
        await db.commit()
    await activate_relations(
        system_sessions,
        monkeypatch,
        missing={("system.user", "nickname", "contains")},
    )
    response = await client.get("/dept/tree-list")
    assert response.status_code == 200, response.text
    assert "leader" not in response.json()["data"]["fieldPolicy"]["query"]


@pytest.mark.parametrize(
    "params",
    [
        {"mode": "update"},
        {"mode": "update", "recordId": "01"},
        {"mode": "update", "recordId": "1.0"},
        {"mode": "create", "recordId": "302"},
        {"mode": "update", "recordId": "302", "parentId": "0"},
        {"mode": "create", "tenantId": "1"},
        {"mode": "update", "recordId": "9223372036854775808"},
    ],
)
async def test_form_query_rejects_ambiguous_or_client_selected_facts(
    http_system, params
):
    client, _ = http_system
    response = await client.get("/role/form-context", params=params)
    assert response.status_code == 422, response.text


async def test_department_root_creation_cannot_borrow_a_bounded_roles_scope(
    http_system,
):
    client, plain = http_system
    async with plain() as db:
        await db.execute(
            update(Role)
            .where(Role.role_id == 201)
            .values(data_scope="5", role_code="R_SELF")
        )
        await db.commit()
    response = await client.get(
        "/dept/form-context", params={"mode": "create", "parentId": "0"}
    )
    assert response.status_code == 200, response.text
    assert response.json()["data"]["fieldPolicy"]["canCreate"] is False
    assert response.json()["data"]["fieldPolicy"]["create"] == []


async def test_masked_contact_snapshot_reports_its_actual_read_level(
    http_system, system_sessions, monkeypatch
):
    client, plain = http_system
    async with plain() as db:
        await db.execute(
            insert(Dept).values(
                tenant_id=0,
                dept_id=401,
                dept_name="Contact",
                status="1",
                phone="13812348000",
            )
        )
        await db.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=501,
                role_name="Masked",
                role_code="R_MASKED",
                data_scope="1",
                status="1",
            )
        )
        await db.execute(
            insert(user_roles).values(tenant_id=0, user_id=101, role_id=501)
        )
        await db.commit()
    await activate_relations(
        system_sessions,
        monkeypatch,
        missing={("system.dept", "phone", "read")},
        extra_grants=(
            Grant(0, "501", FieldRef("system.dept", "phone"), READ, level="masked"),
        ),
    )
    response = await client.get(
        "/dept/form-context", params={"mode": "update", "recordId": "401"}
    )
    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["record"]["phone"] == "138****8000"
    assert data["fieldPolicy"]["readLevels"]["phone"] == "masked"
    assert "phone" in data["fieldPolicy"]["update"]
