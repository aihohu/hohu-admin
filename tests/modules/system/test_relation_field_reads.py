"""Real HTTP reads cannot bypass relation grants or their query dependencies."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, insert, select, update

from app.core.exceptions import BusinessException
from app.core.field_policy import FieldRef, Grant, RecordContext, compile_rules
from app.db.authorization_plan_session import _known_read
from app.db.authorization_runtime import AuthorizationRuntime
from app.db.base import role_depts, role_menus, user_depts, user_roles
from app.modules.ai.api import role_agent as role_agent_api
from app.modules.auth import api as auth_api
from app.modules.system.api import menu as menu_api
from app.modules.system.field_policy.ordinary_catalog import (
    production_catalog as relation_catalog,
)
from app.modules.system.field_policy.relation_catalog import READ, SORT
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.schemas.relation_projection import (
    ProjectedDeptOut,
    ProjectedMenuOut,
)
from app.modules.system.schemas.user import UserPatch
from app.modules.system.schemas.user_projection import ProjectedUserOut
from app.modules.system.service.relation_read_context import RelationReadContext
from app.modules.system.service.relation_read_service import local_tree
from tests.modules.system.test_authorization_system_candidate import TENANT, epoch
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


def test_relation_arrays_allow_aligned_hidden_slots():
    value = ProjectedUserOut(
        user_id="101",
        roles=[None, "R_USER"],
        role_names=[None, "Member"],
        role_name_keys=[None, None],
    )
    assert value.model_dump(by_alias=True)["roles"] == [None, "R_USER"]


def test_selector_actions_keep_their_own_function_gate():
    action = relation_catalog().actions["role.assignable-read"]
    assert action.permission_codes == ("system:user:role-auth",)


def source_context():
    catalog = relation_catalog()
    rules = compile_rules(
        catalog, (Grant(0, "201", FieldRef("system.role", "role_name"), READ),)
    )
    return RelationReadContext(
        SimpleNamespace(tenant_id=0),
        SimpleNamespace(user_id=101),
        rules,
        {"201": None},
        frozenset({"system:user:list"}),
        "user.list-read",
    )


def test_source_evidence_cannot_relabel_another_tenant():
    context = source_context()
    with pytest.raises(BusinessException):
        context.source(
            RecordContext(
                1, "system.role", "301", frozenset({"201"}), context.permissions
            ),
            ("role_name",),
        )


async def test_query_proof_uses_the_existing_strict_read_grammar():
    class Probe:
        async def scalar(self, statement):
            assert _known_read(statement)
            return False

    context = source_context()
    await context.prove(
        Probe(),
        "system.role",
        Role.__table__,
        (("role_code", SORT),),
        readable=("role_name",),
    )


@pytest.mark.parametrize("counter", ["source_count", "source_bytes"])
def test_source_budget_fails_closed(counter):
    context = source_context()
    setattr(context, counter, 16384 if counter == "source_count" else 4 * 1024 * 1024)
    with pytest.raises(BusinessException):
        context.source(context.record("system.role", "301", ("201",)), ("role_name",))


def test_sparse_tree_dto_keeps_real_null_and_omits_hidden_fields():
    value = ProjectedDeptOut(
        dept_id="401", phone=None, children=[{"dept_id": "402"}]
    ).model_dump(mode="json", by_alias=True)
    assert value == {"deptId": "401", "phone": None, "children": [{"deptId": "402"}]}
    with pytest.raises(ValidationError):
        ProjectedMenuOut(menu_id="1001", hashed_password="leak")


def test_tree_rebuilds_local_roots_and_ignores_raw_ancestors():
    rows = [
        {
            "dept_id": "401",
            "dept_name": "Local",
            "parent_id": "999",
            "ancestors": "0,888,999",
        },
        {
            "dept_id": "402",
            "dept_name": "Child",
            "parent_id": "401",
            "ancestors": "0,888,999,401",
        },
    ]
    value = local_tree(rows, anchor="dept_id")
    assert value[0]["parent_id"] is None and value[0]["ancestors"] == "0"
    assert value[0]["children"][0]["ancestors"] == "0,401"


@pytest.mark.parametrize("length", [2, 17])
def test_tree_rejects_cycles_and_excess_depth(length):
    rows = [
        {
            "dept_id": str(i),
            "dept_name": "Node",
            "parent_id": str(i - 1)
            if i > 1
            else (str(length) if length == 2 else None),
        }
        for i in range(1, length + 1)
    ]
    with pytest.raises(BusinessException):
        local_tree(rows, anchor="dept_id")


def test_selector_cannot_omit_hidden_labels_to_claim_a_complete_set():
    with pytest.raises(BusinessException):
        local_tree([{"dept_id": "401"}], anchor="dept_id", options=True)


async def test_role_hidden_name_and_i18n_are_absent(
    http_system, system_sessions, monkeypatch
):
    client, _ = http_system
    await activate_relations(
        system_sessions, monkeypatch, missing=(("system.role", "role_name", "read"),)
    )
    response = await client.get("/role/list")
    assert response.status_code == 200
    rows = response.json()["data"]["records"]
    assert rows and all("roleName" not in row and "i18nKeys" not in row for row in rows)


async def test_role_sort_proved_before_unmatched_filter(
    http_system, system_sessions, monkeypatch
):
    client, _ = http_system
    await activate_relations(
        system_sessions, monkeypatch, missing=(("system.role", "role_code", "sort"),)
    )
    response = await client.get("/role/list", params={"roleName": "nothing-matches"})
    assert response.status_code == 403
    assert response.json()["errorCode"] == "FIELD_POLICY_DENIED"


async def test_historical_leader_has_no_trusted_display(http_system):
    client, plain = http_system
    async with plain() as db:
        await db.execute(
            insert(Dept).values(
                tenant_id=0,
                dept_id=401,
                dept_name="Team",
                status="1",
                leader="untrusted old text",
            )
        )
        await db.commit()
    response = await client.get("/dept/list")
    assert response.status_code == 200
    assert "leader" not in response.json()["data"]["records"][0]


async def test_readable_cleared_leader_keeps_real_null(http_system):
    client, plain = http_system
    async with plain() as db:
        await db.execute(
            insert(Dept).values(
                tenant_id=0, dept_id=401, dept_name="Clear", status="1", leader=None
            )
        )
        await db.commit()
    response = await client.get("/dept/401")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["leader"] is None
    assert "leaderUserId" not in response.json()["data"]


async def test_generic_user_relations_keep_null_slots(
    http_system, system_sessions, monkeypatch
):
    client, plain = http_system
    async with plain() as db:
        await db.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=301,
                role_name="Hidden label",
                role_code="R_MEMBER",
                status="1",
                data_scope="5",
            )
        )
        await db.execute(
            insert(user_roles).values(tenant_id=0, user_id=101, role_id=301)
        )
        await db.commit()
    await activate_relations(
        system_sessions, monkeypatch, missing=(("system.role", "role_name", "read"),)
    )
    response = await client.get("/user/list")
    assert response.status_code == 200, response.text
    row = next(r for r in response.json()["data"]["records"] if r["userId"] == "101")
    assert row["roles"] == ["R_SUPER", "R_MEMBER"]
    assert row["roleNames"] == [None, None]
    assert row["roleNameKeys"] == [None, None]


async def test_generic_profile_keeps_name_semantics(
    http_system, system_sessions, monkeypatch
):
    client, _ = http_system
    await activate_relations(
        system_sessions, monkeypatch, missing=(("system.role", "role_name", "read"),)
    )
    response = await client.get("/user/profile")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["roles"] == [None]


@pytest.mark.parametrize(
    "path", ["/dept/list", "/dept/tree", "/dept/tree-list", "/dept/401"]
)
async def test_leader_requires_both_current_user_source_fields(
    http_system, system_sessions, monkeypatch, path
):
    client, plain = http_system
    async with plain() as db:
        await db.execute(
            insert(Dept).values(
                tenant_id=0,
                dept_id=401,
                dept_name="Team",
                status="1",
                leader="planner",
                leader_user_id=101,
            )
        )
        await db.commit()
    await activate_relations(
        system_sessions, monkeypatch, missing=(("system.user", "nickname", "read"),)
    )
    response = await client.get(path)
    assert response.status_code == 200, response.text
    value = response.json()["data"]
    rows = (
        value["records"]
        if isinstance(value, dict) and "records" in value
        else value
        if isinstance(value, list)
        else [value]
    )
    assert rows and all("leader" not in r and "leaderUserId" not in r for r in rows)


async def test_leader_query_rejects_historical_rows_before_unmatched_filter(
    http_system,
):
    client, plain = http_system
    async with plain() as db:
        await db.execute(
            insert(Dept).values(
                tenant_id=0,
                dept_id=401,
                dept_name="Historical",
                status="1",
                leader="unknown",
            )
        )
        await db.commit()
    response = await client.get("/dept/list", params={"leader": "nothing-matches"})
    assert (
        response.status_code == 403
        and response.json()["errorCode"] == "FIELD_POLICY_DENIED"
    )


async def test_runtime_navigation_ignores_management_field_grants_but_existence_does_not(
    http_system, system_sessions, monkeypatch
):
    client, _ = http_system
    client._transport.app.include_router(auth_api.router, prefix="/auth")
    client._transport.app.include_router(menu_api.router, prefix="/menu")
    await activate_relations(
        system_sessions,
        monkeypatch,
        missing=(
            ("system.menu", "route_name", "exact"),
            ("system.menu", "menu_name", "read"),
        ),
    )
    nav = await client.get("/auth/getUserRoutes")
    assert nav.status_code == 200, nav.text
    denied = await client.get("/auth/isRouteExist", params={"route_name": "not-found"})
    assert (
        denied.status_code == 403
        and denied.json()["errorCode"] == "FIELD_POLICY_DENIED"
    )
    selector = await client.get("/menu/tree-option")
    assert selector.status_code == 403


async def test_protected_reads_reject_stale_current_user_and_do_not_write_epoch(
    http_system,
):
    client, plain = http_system
    async with plain() as db:
        before = await epoch(db)
    assert (await client.get("/role/list")).status_code == 200
    async with plain() as db:
        assert await epoch(db) == before
        await db.execute(update(User).where(User.user_id == 101).values(auth_version=2))
        await db.commit()
    assert (await client.get("/role/list")).status_code == 401


async def test_members_include_disabled_current_members(http_system):
    client, plain = http_system
    async with plain() as db:
        await db.execute(
            insert(Dept).values(tenant_id=0, dept_id=401, dept_name="Team", status="1")
        )
        await db.execute(
            insert(User).values(
                tenant_id=0,
                user_id=501,
                user_name="disabled-member",
                hashed_password="unused",
                status="2",
            )
        )
        await db.execute(
            insert(user_depts).values(
                tenant_id=0, user_id=501, dept_id=401, is_primary="Y"
            )
        )
        await db.commit()
    response = await client.get("/dept/401/users")
    assert response.status_code == 200, response.text
    row = next(r for r in response.json()["data"]["records"] if r["userId"] == "501")
    assert row["isMember"] is True and row["isPrimary"] is True and row["status"] == "2"


async def allow_user_list(db):
    await db.execute(
        insert(Menu).values(
            tenant_id=0,
            menu_id=9001,
            menu_name="List users",
            menu_type="F",
            status="1",
            permission="system:user:list",
        )
    )
    await db.execute(insert(role_menus).values(tenant_id=0, role_id=201, menu_id=9001))


async def test_assignable_selector_keeps_its_original_function_gate(
    http_system, system_sessions, monkeypatch
):
    client, plain = http_system
    await activate_relations(system_sessions, monkeypatch)
    async with plain() as db:
        await db.execute(
            update(Role).where(Role.role_id == 201).values(role_code="R_MANAGER")
        )
        await db.execute(
            delete(role_menus).where(
                role_menus.c.role_id == 201,
                role_menus.c.menu_id.in_(
                    select(Menu.menu_id).where(
                        Menu.tenant_id == 0, Menu.permission == "system:role:list"
                    )
                ),
            )
        )
        await db.commit()
    response = await client.get("/user/assignable-roles")
    assert response.status_code == 200, response.text
    assert any(r["roleId"] == "201" for r in response.json()["data"])
    assert (await client.get("/role/list")).status_code == 403


async def test_user_filter_cannot_borrow_self_query_rights_for_other_rows(
    http_system, system_sessions, monkeypatch
):
    client, plain = http_system
    async with plain() as db:
        await db.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=301,
                role_name="All rows",
                role_code="R_ALL",
                data_scope="1",
                status="1",
            )
        )
        await db.execute(
            insert(user_roles).values(tenant_id=0, user_id=101, role_id=301)
        )
        await db.execute(
            insert(User).values(
                tenant_id=0,
                user_id=501,
                user_name="outside",
                hashed_password="unused",
                status="1",
            )
        )
        await allow_user_list(db)
        await db.commit()
    await activate_relations(
        system_sessions,
        monkeypatch,
        extra_grants=(Grant(0, "301", FieldRef("system.user", "create_time"), READ),),
    )
    async with plain() as db:
        await db.execute(
            update(Role)
            .where(Role.role_id == 201)
            .values(role_code="R_SELF", data_scope="5")
        )
        await db.commit()
    response = await client.get("/user/list", params={"userName": "planner"})
    assert (
        response.status_code == 403
        and response.json()["errorCode"] == "FIELD_QUERY_DENIED"
    )


async def test_department_name_aggregate_is_hidden_if_any_source_is_out_of_scope(
    http_system, system_sessions, monkeypatch
):
    client, plain = http_system
    async with plain() as db:
        await db.execute(
            insert(Dept),
            [
                {"tenant_id": 0, "dept_id": 401, "dept_name": "Visible", "status": "1"},
                {"tenant_id": 0, "dept_id": 402, "dept_name": "Hidden", "status": "1"},
            ],
        )
        await db.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=301,
                role_name="Custom",
                role_code="R_CUSTOM",
                data_scope="2",
                status="1",
            )
        )
        await db.execute(
            insert(user_roles).values(tenant_id=0, user_id=101, role_id=301)
        )
        await db.execute(
            insert(role_depts).values(tenant_id=0, role_id=301, dept_id=401)
        )
        await db.execute(
            insert(user_depts),
            [
                {"tenant_id": 0, "user_id": 101, "dept_id": 401, "is_primary": "Y"},
                {"tenant_id": 0, "user_id": 101, "dept_id": 402, "is_primary": "N"},
            ],
        )
        await allow_user_list(db)
        await db.commit()
    await activate_relations(
        system_sessions,
        monkeypatch,
        extra_grants=(Grant(0, "301", FieldRef("system.dept", "dept_name"), READ),),
    )
    async with plain() as db:
        await db.execute(
            update(Role)
            .where(Role.role_id == 201)
            .values(role_code="R_SELF", data_scope="5")
        )
        await db.commit()
    response = await client.get("/user/list")
    assert response.status_code == 200, response.text
    row = response.json()["data"]["records"][0]
    assert row["deptIds"] == ["401", "402"] and "deptNames" not in row
    assert "Visible" not in str(row) and "Hidden" not in str(row)


async def test_policy_management_title_obeys_role_name_read(
    system_sessions, monkeypatch
):
    _, plain = system_sessions
    await activate_relations(
        system_sessions, monkeypatch, missing=(("system.role", "role_name", "read"),)
    )
    async with plain() as db:
        await db.execute(
            insert(Menu).values(
                tenant_id=0,
                menu_id=9002,
                menu_name="View policies",
                menu_type="F",
                status="1",
                permission="system:role:field-policy-view",
            )
        )
        await db.execute(
            insert(role_menus).values(tenant_id=0, role_id=201, menu_id=9002)
        )
        await db.commit()
    runtime = AuthorizationRuntime(plain.kw["bind"])
    value = await runtime.field_management(
        tenant=TENANT,
        actor_user_version=1,
        catalog=relation_catalog(),
        operation="object",
        payload={},
        role_id=201,
        object_id="system.user",
    )
    assert value["role"]["roleId"] == "201" and "roleName" not in value["role"]


async def test_leader_display_and_query_follow_current_verified_source(
    http_system, system_sessions, monkeypatch
):
    client, plain = http_system
    async with plain() as db:
        await db.execute(
            insert(User).values(
                tenant_id=0,
                user_id=501,
                user_name="leader-account",
                nickname="Old nickname",
                hashed_password="unused",
                status="1",
            )
        )
        await db.execute(
            insert(Dept).values(
                tenant_id=0,
                dept_id=401,
                dept_name="Team",
                status="1",
                leader="Old nickname",
                leader_user_id=501,
            )
        )
        await db.commit()
    await activate_relations(system_sessions, monkeypatch)
    runtime = AuthorizationRuntime(plain.kw["bind"])
    await runtime.system(
        tenant=TENANT,
        actor_user_version=1,
        operation="user.update",
        target_id=501,
        payload=UserPatch(nickname="Fresh nickname"),
    )
    detail = await client.get("/dept/401")
    assert (
        detail.status_code == 200
        and detail.json()["data"]["leader"] == "Fresh nickname"
    )
    result = await client.get("/dept/list", params={"leader": "Old nickname"})
    assert result.status_code == 200 and result.json()["data"]["total"] == 0

    account = await client.get("/dept/list", params={"leader": "leader-account"})
    assert account.status_code == 200 and account.json()["data"]["total"] == 1


async def test_missing_role_agent_binding_keeps_its_public_error_code(http_system):
    client, _ = http_system
    client._transport.app.include_router(role_agent_api.router, prefix="/role-agent")
    response = await client.get("/role-agent/999999999")
    assert response.status_code == 404, response.text
    assert response.json()["errorCode"] == "AI_ROLE_NOT_FOUND"


async def test_user_role_filter_has_a_strict_readable_join(
    http_system, system_sessions, monkeypatch
):
    client, _ = http_system
    await activate_relations(system_sessions, monkeypatch)
    response = await client.get("/user/list", params={"roleCode": "R_SUPER"})
    assert response.status_code == 200, response.text
    assert response.json()["data"]["total"] == 1
