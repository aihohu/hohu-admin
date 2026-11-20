"""Live generic writers: blind writes, source gates, child footprints and HTTP."""

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import event, insert

from app.core.config import settings
from app.core.exceptions import BusinessException
from app.core.field_policy import FieldRef, Grant
from app.db.base import user_roles
from app.modules.system.field_policy.evaluator import FieldGrant
from app.modules.system.field_policy.ordinary_catalog import ordinary_catalog
from app.modules.system.field_policy.relation_catalog import READ
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.schemas.dept import DeptCreate, DeptUpdate
from app.modules.system.schemas.role import RoleUpdate
from app.modules.system.schemas.user import UserPatch
from app.modules.system.service.authorization_configuration_candidate import (
    configuration_authorization_candidate,
)
from tests.modules.system import test_ordinary_field_active as ordinary_fixture
from tests.modules.system.test_active_transfer_delivery import enable
from tests.modules.system.test_authorization_system_candidate import TENANT, epoch, run
from tests.modules.system.test_authorization_system_http import (
    http_system as _http_system,
)
from tests.modules.system.test_ordinary_field_active import (
    activate as activate_ordinary,
)
from tests.modules.system.test_relation_field_writes import activate_relations
from tests.modules.system.test_relation_field_writes import (
    plan_sessions as _plan_sessions,
)
from tests.modules.system.test_relation_field_writes import (
    system_sessions as _system_sessions,
)
from tests.test_release_migration_roundtrip import apply_steps

http_system = _http_system
plan_sessions = _plan_sessions
system_sessions = _system_sessions


@pytest.mark.parametrize(
    "leader,source_granted", [(None, False), ("planner", False), ("planner", True)]
)
async def test_old_user_policy_preserves_relation_protocol_and_checks_sources(
    system_sessions, monkeypatch, leader, source_granted
):
    factory, plain = system_sessions
    monkeypatch.setattr(settings, "USER_FIELD_POLICY_AVAILABLE", True)
    rules = (
        tuple(
            FieldGrant("201", name, operation, purpose)
            for name in ("user_name", "nickname")
            for operation, purpose in (
                ("read", "management.read"),
                ("exact", "management.query"),
            )
        )
        if source_granted
        else ()
    )
    await enable(factory, TENANT, rules)
    async with factory() as db:
        if leader and not source_granted:
            with pytest.raises(BusinessException) as error:
                await run(
                    db,
                    "dept.create",
                    DeptCreate(dept_name="legacy source", status="1", leader=leader),
                )
            assert error.value.error_code == "FIELD_POLICY_DENIED"
            await db.rollback()
        else:
            created = await run(
                db,
                "dept.create",
                DeptCreate(dept_name="legacy source", status="1", leader=leader),
            )
            await db.commit()
            async with plain() as observer:
                assert (await observer.get(Dept, created["id"])).leader_user_id == (
                    101 if leader else None
                )


async def test_only_the_matched_leaders_source_read_scope_is_required(
    system_sessions, monkeypatch
):
    factory, plain = system_sessions
    async with plain() as db:
        await db.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=501,
                role_name="self-read",
                role_code="R_SELF",
                data_scope="5",
                status="1",
            )
        )
        await db.execute(
            insert(user_roles).values(tenant_id=0, role_id=501, user_id=101)
        )
        await db.execute(
            insert(User).values(
                tenant_id=0,
                user_id=901,
                user_name="unreadable",
                status="1",
                hashed_password="unused",
            )
        )
        await db.commit()
    await activate_relations(
        system_sessions,
        monkeypatch,
        missing=(("system.user", "nickname", "read"),),
        extra_grants=(Grant(0, "501", FieldRef("system.user", "nickname"), READ),),
    )
    async with factory() as db:
        created = await run(
            db,
            "dept.create",
            DeptCreate(dept_name="scoped leader", status="1", leader="planner"),
        )
        await db.commit()
    async with plain() as db:
        assert (await db.get(Dept, created["id"])).leader_user_id == 101


@pytest.mark.parametrize("value", [None, "old"])
async def test_supplied_null_or_same_value_still_needs_update(
    system_sessions, monkeypatch, value
):
    factory, plain = system_sessions
    await activate_relations(
        system_sessions, monkeypatch, missing=(("system.role", "role_desc", "update"),)
    )
    async with factory() as db:
        with pytest.raises(BusinessException) as error:
            await run(db, "role.update", RoleUpdate(role_desc=value), target_id=201)
        assert error.value.error_code == "FIELD_POLICY_DENIED"
        await db.rollback()


async def test_default_dept_field_cannot_be_omitted_to_bypass_grant(
    system_sessions, monkeypatch
):
    factory, _ = system_sessions
    await activate_relations(
        system_sessions, monkeypatch, missing=(("system.dept", "phone", "create"),)
    )
    async with factory() as db:
        with pytest.raises(BusinessException) as error:
            await run(db, "dept.create", DeptCreate(dept_name="team", status="1"))
        assert error.value.error_code == "FIELD_POLICY_DENIED"
        await db.rollback()


async def test_leader_query_is_proved_before_matching(system_sessions, monkeypatch):
    factory, plain = system_sessions
    await activate_relations(
        system_sessions, monkeypatch, missing=(("system.user", "user_name", "exact"),)
    )
    sql = []

    def observed(_c, _cur, statement, _p, _ctx, _many):
        sql.append(statement)

    event.listen(plain.kw["bind"].sync_engine, "before_cursor_execute", observed)
    try:
        async with factory() as db:
            with pytest.raises(BusinessException) as error:
                await run(
                    db,
                    "dept.create",
                    DeptCreate(dept_name="team", status="1", leader="does-not-exist"),
                )
            assert error.value.error_code == "FIELD_POLICY_DENIED"
            await db.rollback()
    finally:
        event.remove(plain.kw["bind"].sync_engine, "before_cursor_execute", observed)
    assert not any("lower(" in statement for statement in sql)


async def test_leader_binding_survives_unrelated_patch_then_clears_atomically(
    system_sessions, monkeypatch, record_property
):
    factory, plain = system_sessions
    await activate_relations(system_sessions, monkeypatch)
    async with factory() as db:
        created = await run(
            db,
            "dept.create",
            DeptCreate(dept_name="team", status="1", leader="planner"),
        )
        for name, value in created["io"].items():
            record_property("active_leader_create_" + name, value)
        await db.commit()
    async with factory() as db:
        await run(
            db, "dept.update", DeptUpdate(phone="13800138000"), target_id=created["id"]
        )
        await db.commit()
    async with plain() as db:
        dept = await db.get(Dept, created["id"])
        assert (dept.leader, dept.leader_user_id) == ("planner", 101)
    async with factory() as db:
        await run(db, "dept.update", DeptUpdate(leader=None), target_id=created["id"])
        await db.commit()
    async with plain() as db:
        dept = await db.get(Dept, created["id"])
        assert dept.leader is None and dept.leader_user_id is None


async def test_referenced_user_delete_refuses_before_any_dml(system_sessions):
    factory, plain = system_sessions
    async with plain() as db:
        await db.execute(
            insert(Dept).values(
                tenant_id=0,
                dept_id=401,
                dept_name="team",
                status="1",
                leader="planner",
                leader_user_id=101,
            )
        )
        await db.commit()
    dml = []

    def observed(_c, _cur, statement, _p, _ctx, _many):
        if statement.lstrip().startswith(("INSERT", "UPDATE", "DELETE")):
            dml.append(statement)

    event.listen(plain.kw["bind"].sync_engine, "before_cursor_execute", observed)
    try:
        async with factory() as db:
            with pytest.raises(BusinessException) as error:
                await run(db, "user.delete", target_id=101)
            assert error.value.error_code == "USER_DELETE_REFERENCED"
            await db.rollback()
    finally:
        event.remove(plain.kw["bind"].sync_engine, "before_cursor_execute", observed)
    assert dml == []


@pytest.mark.parametrize(
    "missing,body",
    [
        (
            "menu_name",
            {"icon": "new", "buttons": [{"code": "demo:edit", "desc": "New button"}]},
        ),
        ("i18n_key", {"menu_name": "New name"}),
    ],
)
async def test_menu_actual_child_and_implicit_fields_are_authorized(
    system_sessions, monkeypatch, missing, body
):
    factory, plain = system_sessions
    async with plain() as db:
        await db.execute(
            insert(Menu).values(
                tenant_id=0,
                menu_id=901,
                menu_name="Original",
                menu_type="C",
                status="1",
                i18n_key="route.original",
            )
        )
        await db.execute(
            insert(Menu).values(
                tenant_id=0,
                menu_id=902,
                parent_id=901,
                menu_name="Old button",
                menu_type="F",
                status="1",
                permission="demo:edit",
            )
        )
        await db.commit()
    await activate_relations(
        system_sessions, monkeypatch, missing=(("system.menu", missing, "update"),)
    )
    async with plain() as db:
        before = await epoch(db)
    async with factory() as db:
        with pytest.raises(BusinessException) as error:
            await configuration_authorization_candidate.execute(
                db,
                tenant=TENANT,
                actor_user_version=1,
                operation="menu.update",
                target_id=901,
                payload=body,
            )
        assert error.value.error_code == "FIELD_POLICY_DENIED"
        await db.rollback()
    async with plain() as db:
        assert (await db.get(Menu, 901)).menu_name == "Original"
        assert (await db.get(Menu, 902)).menu_name == "Old button"
        assert await epoch(db) == before


async def test_real_http_blind_user_patch_and_forbidden_same_field(
    http_system, system_sessions, monkeypatch
):
    client, plain = http_system
    await activate_relations(
        system_sessions,
        monkeypatch,
        missing=(
            ("system.user", "nickname", "read"),
            ("system.user", "user_phone", "update"),
        ),
    )
    success = await client.patch("/user/101", json={"nickname": "Blind update"})
    assert success.status_code == 200, success.text
    assert success.json()["data"] is None
    denied = await client.patch("/user/101", json={"userPhone": None})
    assert denied.status_code == 403, denied.text
    async with plain() as db:
        assert (await db.get(User, 101)).nickname == "Blind update"


@pytest.mark.parametrize(
    "operation,payload",
    [
        ("role.update", RoleUpdate(role_desc="blocked")),
        ("user.update", UserPatch(nickname="blocked")),
    ],
)
async def test_four_object_active_head_cannot_fall_back_to_legacy_writes(
    system_sessions, monkeypatch, operation, payload
):
    factory, _ = system_sessions
    with monkeypatch.context() as source:
        source.setattr(ordinary_fixture, "production_catalog", ordinary_catalog)
        await activate_ordinary(system_sessions, monkeypatch)
    async with factory() as db:
        with pytest.raises(BusinessException) as error:
            await run(
                db,
                operation,
                payload,
                target_id=201 if operation.startswith("role") else 101,
            )
        assert error.value.error_code == "FIELD_POLICY_UNAVAILABLE"
        await db.rollback()


async def test_active_department_policy_blocks_source_downgrade(
    system_sessions, monkeypatch
):
    _, plain = system_sessions
    await activate_relations(system_sessions, monkeypatch)
    module = (
        ScriptDirectory.from_config(Config("alembic.ini"))
        .get_revision("e020f2b00001")
        .module
    )
    async with plain.kw["bind"].begin() as db:
        with pytest.raises(RuntimeError, match="FIELD_POLICY_DOWNGRADE_BLOCKED"):
            await db.run_sync(apply_steps, [module], "downgrade")
