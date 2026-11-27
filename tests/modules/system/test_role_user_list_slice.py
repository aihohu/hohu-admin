"""The ordinary HTTP control plane and the user list share the active facts."""

import asyncio
from datetime import UTC, datetime, timedelta
from time import perf_counter
from unittest.mock import AsyncMock
from uuid import uuid4

import casbin
from sqlalchemy import event, insert, select, update

from app.core.field_policy.documents import catalog_parts
from app.core.field_policy.qualification import Qualification
from app.core.security import create_access_token
from app.db import authorization_field_management_owner as management
from app.db.base import role_menus, user_depts, user_roles
from app.modules.auth import service
from app.modules.system.api.field_management import create_management_router
from app.modules.system.field_policy.ordinary_catalog import production_catalog
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User
from app.modules.system.service import relation_user_read
from app.modules.system.service.authorization_read_budget import _read_phase
from tests.modules.system.test_action_field_scope_http import (
    http_system as http_system,
)
from tests.modules.system.test_action_field_scope_http import (
    parameter_http as parameter_http,
)
from tests.modules.system.test_action_field_scope_http import (
    parameters as parameters,
)
from tests.modules.system.test_action_field_scope_http import (
    plan_sessions as plan_sessions,
)
from tests.modules.system.test_action_field_scope_http import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_action_field_scope_http import (
    tenant_id as tenant_id,
)
from tests.modules.system.test_action_field_scope_http import (
    token,
)
from tests.modules.system.test_relation_field_writes import activate_relations


async def seed_manager_and_users(plain, tid, *, extra_permissions=()):
    manager = 2001 + tid
    async with plain() as db:
        await db.execute(
            insert(Role).values(
                tenant_id=tid,
                role_id=manager,
                role_code="R_SUPER",
                role_name="manager",
                status="1",
            )
        )
        await db.execute(
            insert(User).values(
                tenant_id=tid,
                user_id=manager,
                user_name="manager",
                hashed_password="fixture",
                status="1",
            )
        )
        await db.execute(
            insert(user_roles).values(tenant_id=tid, user_id=manager, role_id=manager)
        )
        codes = tuple(
            f"system:role:field-policy-{perm}" for perm in ("view", "edit", "activate")
        )
        for idx, code in enumerate(codes + extra_permissions):
            mid = await db.scalar(
                select(Menu.menu_id).where(
                    Menu.tenant_id == tid, Menu.permission == code
                )
            )
            if mid is None:
                mid = 10220 + idx
                await db.execute(
                    insert(Menu).values(
                        tenant_id=tid,
                        menu_id=mid,
                        menu_name=code,
                        menu_type="F",
                        permission=code,
                        status="1",
                    )
                )
            await db.execute(
                insert(role_menus).values(tenant_id=tid, role_id=manager, menu_id=mid)
            )
        await db.execute(
            insert(User),
            [
                {
                    "tenant_id": tid,
                    "user_id": 12000 + i,
                    "user_name": f"member{i:03}",
                    "nickname": None,
                    "hashed_password": "fixture",
                    "status": "1",
                }
                for i in range(101)
            ],
        )
        await db.commit()
    return manager


async def verify_pages_and_cost(client, plain, monkeypatch, record_property):
    counts = []
    auth_counts = []
    calls = []
    elapsed = []
    engine_calls = []
    original_enforce = casbin.Enforcer.enforce

    def count_enforce(enforcer, *args):
        engine_calls.append(1)
        return original_enforce(enforcer, *args)

    monkeypatch.setattr(casbin.Enforcer, "enforce", count_enforce)
    statements = []

    def capture(_conn, _cur, stmt, _params, _ctx, _many):
        phase = _read_phase.get()
        label = phase[2] if phase else None
        business = label in {"projection", "source"} or "pg_catalog.count" in stmt
        statements.append((label, stmt, not business))

    for size in (1, 10, 100):
        statements.clear()
        engine_calls.clear()
        start = perf_counter()
        engine = plain.kw["bind"].sync_engine
        event.listen(engine, "before_cursor_execute", capture)
        try:
            response = await client.get(
                "/user/list", params={"userName": "member", "size": size}
            )
        finally:
            event.remove(engine, "before_cursor_execute", capture)
        assert response.status_code == 200, response.text
        assert not any(
            label is None and "sys_user" in stmt for label, stmt, _ in statements
        ), "list entry must not preload an actor outside the authorization barriers"
        identity = [stmt for label, stmt, _ in statements if label == "identity"]
        assert 1 <= len(identity) <= 3
        assert sum("FROM sys_user " in sql.replace("\n", " ") for sql in identity) == 1
        assert all(
            "sys_dept" not in sql and "sys_user_dept" not in sql for sql in identity
        )
        assert all(label != "source" for label, _, _ in statements)
        manifest_reads = [stmt for label, stmt, _ in statements if label == "manifest"]
        for table in ("sys_field_policy_head", "sys_field_policy_object_state"):
            assert (
                sum(
                    f"FROM {table} " in sql.replace("\n", " ") for sql in manifest_reads
                )
                == 1
            ), table
        page = response.json()["data"]
        assert page["total"] == 101 and len(page["records"]) == size
        assert all(
            set(row) == {"userId", "userName", "nickname", "fieldAccess"}
            and row["nickname"] is None
            for row in page["records"]
        )
        elapsed.append(round((perf_counter() - start) * 1000, 1))
        calls.append(len(engine_calls))
        counts.append(len(statements))
        auth_counts.append(sum(auth for _, _, auth in statements))
    assert counts[0] > 0 and len(set(counts)) == 1, counts
    record_property("sql_all_1_10_100", str(counts))
    assert calls[0] > 0 and len(set(calls)) == 1, calls
    record_property("casbin_calls_1_10_100", str(calls))
    record_property("http_ms_1_10_100", str(elapsed))
    assert 0 < auth_counts[0] < counts[0] and len(set(auth_counts)) == 1, auth_counts
    record_property("authorization_sql_1_10_100", str(auth_counts))
    assert (
        await client.get("/user/list", params={"status": "1", "current": 999})
    ).status_code == 403
    page2 = (
        await client.get(
            "/user/list", params={"userName": "member", "current": 2, "size": 100}
        )
    ).json()["data"]
    assert page2["total"] == 101 and len(page2["records"]) == 1
    assert page2["records"][0]["userId"] not in {r["userId"] for r in page["records"]}


async def verify_role_scopes(client, plain, tid, role, admin):
    # A different role grants a broad scope, but has no user-list action.
    # It must not extend this subject's self-only authorization.
    async with plain() as db:
        await db.execute(
            update(Role)
            .where(Role.tenant_id == tid, Role.role_id == role)
            .values(data_scope="5")
        )
        await db.execute(
            insert(Role).values(
                tenant_id=tid,
                role_id=2400,
                role_name="Other tasks",
                role_code="OTHER",
                status="1",
                data_scope="1",
            )
        )
        await db.execute(
            insert(user_roles).values(tenant_id=tid, user_id=101 + tid, role_id=2400)
        )
        await db.commit()
    response = await client.get("/user/list")
    assert response.status_code == 200, response.text
    assert response.json()["data"]["total"] == 1
    assert response.json()["data"]["records"][0]["userId"] == str(101 + tid)
    # Binding the new role to this action supplies ordinary default fields, with
    # the original role's explicit deny still applying to the actor's own row.
    async with plain() as db:
        mid = await db.scalar(
            select(Menu.menu_id).where(
                Menu.tenant_id == tid, Menu.permission == "system:user:list"
            )
        )
        await db.execute(
            insert(role_menus).values(tenant_id=tid, role_id=2400, menu_id=mid)
        )
        await db.commit()
    new_role = await admin("/roles/2400/objects/system.user")
    assert new_role["listPolicy"] == {
        "mode": "restrict_selected",
        "read": [],
        "query": [],
    }
    records = (await client.get("/user/list", params={"userName": "planner"})).json()[
        "data"
    ]["records"]
    assert [r["userId"] for r in records] == [
        str(101 + tid)
    ] and "nickname" not in records[0]
    other = (await client.get("/user/list", params={"userName": "member001"})).json()[
        "data"
    ]["records"]
    assert len(other) == 1 and "nickname" in other[0] and "userPhone" not in other[0]


async def test_role_configuration_to_paginated_users(
    tenant_id, parameters, parameter_http, monkeypatch, record_property
):
    client, plain = parameter_http
    tid, role = tenant_id, 201 + tenant_id
    catalog = production_catalog()
    fresh = await client.get("/user/list")
    assert fresh.status_code == 200, fresh.text
    assert all(
        "userName" in row and "userPhone" not in row and "userEmail" not in row
        for row in fresh.json()["data"]["records"]
    )
    await activate_relations(parameters, monkeypatch, tenant_id=tid, catalog=catalog)
    await activate_relations(
        parameters, monkeypatch, tenant_id=1 - tid, catalog=catalog
    )
    client._transport.app.include_router(
        create_management_router(), prefix="/system/field-policy"
    )
    manager = await seed_manager_and_users(plain, tid)
    proof = Qualification(
        "b" * 40,
        catalog.digest,
        *catalog_parts(catalog),
        "c" * 64,
        datetime.now(UTC) + timedelta(minutes=20),
    )
    monkeypatch.setattr(management, "read_server_qualification", lambda **_: proof)
    jwt = create_access_token(
        subject=str(manager), tenant_id=tid, tenant_version=1, user_version=1
    )
    headers = {"Authorization": f"Bearer {jwt}"}
    base = "/system/field-policy"
    obj = f"/roles/{role}/objects/system.user"

    async def admin(path, body=None):
        r = await client.request(
            "GET" if body is None else "POST", base + path, headers=headers, json=body
        )
        assert r.status_code == 200, r.text
        return r.json()["data"]

    def mutation():
        return {"requestId": str(uuid4()), "reason": "role user list regression"}

    async def activate_saved():
        state = await admin("/manifests/current")
        target = {
            "expectedVersion": state["headVersion"],
            "catalogDigest": catalog.digest,
        }
        preview = await admin("/manifests/preview", target)
        await admin(
            "/manifests",
            target | mutation() | {"previewDigest": preview["previewDigest"]},
        )
        for operation in ("ready", "activate"):
            state = await admin("/manifests/current")
            await admin(
                f"/manifests/{state['candidateManifestId']}/{operation}",
                {
                    "expectedVersion": state["headVersion"],
                    "expectedEpoch": state["epoch"],
                    "catalogDigest": catalog.digest,
                    "candidateManifestDigest": state["candidateManifestDigest"],
                }
                | mutation(),
            )

    document = await admin(obj)
    draft = {
        "catalogDigest": catalog.digest,
        "expectedVersion": document["headVersion"],
        "expectedBindingVersion": document["bindingVersion"],
        "rules": document.get("listRemainder", []),
        "listPolicy": {
            "mode": "allow_selected",
            "read": ["nickname", "user_name"],
            "query": ["user_name"],
        },
    }
    preview = await admin(obj + "/preview", draft)
    await admin(
        obj + "/revisions",
        draft | mutation() | {"previewDigest": preview["previewDigest"]},
    )
    reopened = await admin(obj)
    assert reopened["listPolicy"] == draft["listPolicy"]
    assert reopened["listRemainder"] == document["listRemainder"]
    assert any(
        rule["access"]["purpose"] == "management.update"
        for rule in reopened["listRemainder"]
    )
    before = await client.get("/user/list")
    assert before.status_code == 200 and "status" in before.json()["data"]["records"][0]
    await activate_saved()

    await verify_pages_and_cost(client, plain, monkeypatch, record_property)
    foreign = await client.get(
        base + f"/roles/{202 - tid}/objects/system.user", headers=headers
    )
    assert foreign.status_code == 403
    next_doc = await admin(obj)
    restriction = {
        "mode": "restrict_selected",
        "read": ["nickname"],
        "query": ["status"],
    }
    draft.update(
        expectedVersion=next_doc["headVersion"],
        expectedBindingVersion=next_doc["bindingVersion"],
        listPolicy=restriction,
    )
    preview = await admin(obj + "/preview", draft)
    await admin(
        obj + "/revisions",
        draft | mutation() | {"previewDigest": preview["previewDigest"]},
    )
    assert "nickname" in (await client.get("/user/list")).json()["data"]["records"][0]
    await activate_saved()
    rows = (await client.get("/user/list")).json()["data"]["records"]
    assert all(
        "nickname" not in row and "status" in row and "userPhone" not in row
        for row in rows
    )
    assert (await client.get("/user/list", params={"status": "1"})).status_code == 403

    await verify_role_scopes(client, plain, tid, role, admin)
    state = await admin("/manifests/current")
    await admin(
        "/freeze",
        {
            "expectedVersion": state["headVersion"],
            "expectedEpoch": state["epoch"],
            "objectId": "system.user",
        }
        | mutation(),
    )
    frozen = await client.get("/user/list")
    assert (
        frozen.status_code == 503
        and frozen.json()["errorCode"] == "FIELD_POLICY_UNAVAILABLE"
    )
    assert (await admin(obj))["copyFields"] is None
    unaffected = await client.get("/user/list", headers=token(1 - tid))
    assert unaffected.status_code == 200, unaffected.text
    peer_rows = unaffected.json()["data"]["records"]
    assert [r["userId"] for r in peer_rows] == [str(102 - tid)]
    assert "nickname" in peer_rows[0] and "userPhone" in peer_rows[0]


async def test_list_follows_department_and_menu_revocation(
    tenant_id, parameter_http, monkeypatch, record_property
):
    client, plain = parameter_http
    tid, actor, role = tenant_id, 101 + tenant_id, 201 + tenant_id
    manager = await seed_manager_and_users(
        plain, tid, extra_permissions=("system:role:menu-auth",)
    )
    initial = await client.get("/user/list")
    assert initial.status_code == 200 and initial.json()["data"]["total"] > 2
    async with plain() as db:
        await db.execute(
            insert(Dept).values(
                tenant_id=tid, dept_id=721, dept_name="Local", status="1"
            )
        )
        await db.execute(
            insert(user_depts),
            [
                {"tenant_id": tid, "dept_id": 721, "user_id": uid, "is_primary": "Y"}
                for uid in (actor, 12000)
            ],
        )
        await db.execute(
            update(Role)
            .where(Role.tenant_id == tid, Role.role_id == role)
            .values(data_scope="3")
        )
        await db.commit()
    scopes = []

    def capture(_conn, _cur, stmt, _params, _ctx, _many):
        phase = _read_phase.get()
        if phase and phase[2] == "scope":
            scopes.append(stmt)

    engine = plain.kw["bind"].sync_engine
    event.listen(engine, "before_cursor_execute", capture)
    try:
        scoped = await client.get("/user/list")
    finally:
        event.remove(engine, "before_cursor_execute", capture)
    assert scoped.status_code == 200, scoped.text
    page = scoped.json()["data"]
    assert page["total"] == 2 and {r["userId"] for r in page["records"]} == {
        str(actor),
        "12000",
    }
    assert len(scopes) == 1 and "sys_user_dept" in scopes[0]
    manager_token = create_access_token(
        subject=str(manager), tenant_id=tid, tenant_version=1, user_version=1
    )
    entered, writing = asyncio.Event(), asyncio.Event()
    proof = relation_user_read.query_capabilities
    lock_times = []

    async def overlapping_proof(*args, **kwargs):
        entered.set()
        await asyncio.wait_for(writing.wait(), 5)
        return await proof(*args, **kwargs)

    def before_lock(_conn, _cursor, sql, *_):
        if (
            "FROM sys_user_field_policy_state " in sql.replace("\n", " ")
            and "FOR UPDATE" in sql
            and not writing.is_set()
        ):
            lock_times.append(perf_counter())
            writing.set()

    def after_lock(_conn, _cursor, sql, *_):
        if (
            len(lock_times) == 1
            and "FROM sys_user_field_policy_state " in sql.replace("\n", " ")
            and "FOR UPDATE" in sql
        ):
            lock_times.append(perf_counter())

    with monkeypatch.context() as patch:
        patch.setattr(relation_user_read, "query_capabilities", overlapping_proof)
        event.listen(engine, "before_cursor_execute", before_lock)
        event.listen(engine, "after_cursor_execute", after_lock)
        reader = asyncio.create_task(client.get("/user/list"))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            changed = await client.put(
                f"/role/menu/{role}",
                json=[],
                headers={"Authorization": f"Bearer {manager_token}"},
            )
            old = await reader
        finally:
            event.remove(engine, "before_cursor_execute", before_lock)
            event.remove(engine, "after_cursor_execute", after_lock)
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
    assert old.status_code == 200 and old.json()["data"]["total"] == 2
    assert changed.status_code == 200, changed.text
    assert len(lock_times) == 2
    record_property(
        "menu_writer_barrier_sql_ms", round(1000 * (lock_times[1] - lock_times[0]), 2)
    )
    denied = await client.get("/user/list")
    assert (
        denied.status_code == 403 and denied.json()["errorCode"] == "MISSING_PERMISSION"
    )
    assert (await client.get("/user/list", headers=token(1 - tid))).status_code == 200


async def test_list_token_checks_live_status_and_versions(
    tenant_id, parameter_http, monkeypatch
):
    client, plain = parameter_http
    tid, uid = tenant_id, 101 + tenant_id

    async def refused(status, error):
        result = await client.get("/user/list")
        assert (result.status_code, result.json()["errorCode"]) == (status, error)

    async with plain() as db:
        account = update(User).where(User.tenant_id == tid, User.user_id == uid)
        tenant = update(Tenant).where(Tenant.tenant_id == tid)
        await db.execute(account.values(status="2"))
        await db.commit()
        await refused(403, "ACCOUNT_DISABLED")
        await db.execute(account.values(status="1"))
        await db.execute(tenant.values(status="2", lifecycle_state="disabled"))
        await db.commit()
        await refused(403, "TENANT_DISABLED")
        await db.execute(
            tenant.values(status="1", lifecycle_state="active", row_version=2)
        )
        await db.commit()
        await refused(401, "TOKEN_EXPIRED")
        await db.execute(tenant.values(row_version=1))
        await db.execute(account.values(auth_version=2))
        await db.commit()
        await refused(401, "TOKEN_EXPIRED")
    deny = AsyncMock(return_value=True)
    monkeypatch.setattr(service, "_is_blacklisted", deny)
    await refused(401, "TOKEN_EXPIRED")
    deny.assert_awaited_once()
