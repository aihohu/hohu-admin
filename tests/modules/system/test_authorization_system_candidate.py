"""Candidate System owners use complete plans before any business mutation."""

import pytest
from sqlalchemy import event, insert, select, update

from app.core.authorization_plan import ResourceKind
from app.core.exceptions import BusinessException
from app.core.tenant import TenantContext
from app.db.base import Base, role_menus, user_roles
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.models.user_field_policy import UserFieldPolicyState
from app.modules.system.schemas.dept import DeptCreate, DeptUpdate
from app.modules.system.schemas.role import RoleCreate, RoleUpdate
from app.modules.system.schemas.user import UpdateProfile, UserCreate, UserUpdate
from app.modules.system.service.authorization_resource_catalog import resource_spec
from app.modules.system.service.authorization_system_candidate import (
    SYSTEM_CANDIDATE_ENTRIES,
    system_authorization_candidate,
)
from app.modules.system.service.user_department_assignment_service import (
    user_department_assignment_service,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)

TENANT = TenantContext(0, "plan-0", 101, 1, "access_token")


@pytest.fixture
async def system_sessions(plan_sessions):
    _, plain = plan_sessions
    for kind in ResourceKind:
        resource_spec(kind)
    async with plain.kw["bind"].begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with plain() as db:
        await db.execute(
            update(Role).where(Role.role_id == 201).values(role_code="R_SUPER")
        )
        await db.execute(
            insert(user_roles).values(tenant_id=0, user_id=101, role_id=201)
        )
        permissions = sorted(
            {
                p
                for entry in SYSTEM_CANDIDATE_ENTRIES.values()
                for p in entry.permissions
            }
        )
        for index, permission in enumerate(permissions, 1001):
            await db.execute(
                insert(Menu).values(
                    tenant_id=0,
                    menu_id=index,
                    menu_name=permission,
                    menu_type="F",
                    status="1",
                    permission=permission,
                )
            )
            await db.execute(
                insert(role_menus).values(tenant_id=0, role_id=201, menu_id=index)
            )
        await db.commit()
    return plan_sessions


async def run(db, operation, payload=None, target_id=None, **kwargs):
    return await system_authorization_candidate.execute(
        db,
        tenant=TENANT,
        actor_user_version=1,
        operation=operation,
        payload=payload,
        target_id=target_id,
        **kwargs,
    )


async def epoch(db):
    return await db.scalar(
        select(UserFieldPolicyState.epoch).where(UserFieldPolicyState.tenant_id == 0)
    )


async def test_profile_current_version_noop_and_ready_invalidation(system_sessions):
    factory, plain = system_sessions
    async with plain() as setup:
        await setup.execute(
            update(UserFieldPolicyState)
            .where(UserFieldPolicyState.tenant_id == 0)
            .values(mode="ready", candidate_manifest_digest="a" * 64, epoch=2)
        )
        await setup.commit()
    async with factory() as db:
        await run(db, "user.profile", UpdateProfile(nickname="fresh"))
        await db.commit()
    async with plain() as observer:
        state = await observer.scalar(
            select(UserFieldPolicyState).where(UserFieldPolicyState.tenant_id == 0)
        )
        assert (state.epoch, state.mode, state.candidate_manifest_digest) == (
            3,
            "legacy",
            "a" * 64,
        )
    async with factory() as db:
        await run(db, "user.profile", UpdateProfile(nickname="fresh"))
        await db.commit()
    async with plain() as observer:
        assert await epoch(observer) == 3


async def test_stale_token_and_plain_session_cannot_write(system_sessions):
    factory, plain = system_sessions
    async with plain() as db:
        with pytest.raises(BusinessException):
            await run(db, "user.profile", UpdateProfile(nickname="forbidden"))
    async with factory() as db:
        with pytest.raises(BusinessException) as error:
            await system_authorization_candidate.execute(
                db,
                tenant=TENANT,
                actor_user_version=999,
                operation="user.profile",
                payload=UpdateProfile(nickname="forbidden"),
            )
        assert error.value.error_code == "INVALID_CREDENTIALS"
        await db.rollback()
    async with plain() as db:
        assert await db.scalar(select(User.nickname).where(User.user_id == 101)) is None
        assert await epoch(db) == 1


async def test_create_locks_roles_depts_and_settings_before_user_insert(
    system_sessions,
):
    factory, plain = system_sessions
    async with plain() as setup:
        await setup.execute(
            insert(Role).values(
                tenant_id=0,
                role_id=301,
                role_name="Member",
                role_code="R_USER",
                data_scope="5",
                status="1",
            )
        )
        await setup.execute(
            insert(Dept).values(
                tenant_id=0, dept_id=401, dept_name="Team", ancestors="0", status="1"
            )
        )
        await setup.commit()
    sql = []

    def record(_c, _cursor, statement, _params, _context, _many):
        sql.append(statement)

    event.listen(plain.kw["bind"].sync_engine, "before_cursor_execute", record)
    try:
        async with factory() as db:
            result = await run(
                db,
                "user.create",
                UserCreate(
                    user_name="candidate",
                    password="TestStrong123!",
                    nickname="Candidate",
                    status="1",
                    role_ids=["301"],
                    dept_ids=[{"deptId": "401", "isPrimary": True}],
                ),
            )
            await db.commit()
        first_insert = next(
            i
            for i, statement in enumerate(sql)
            if statement.startswith("INSERT INTO sys_user ")
        )
        for table in (
            "sys_user_field_policy_global_guard",
            "sys_user_field_policy_state",
            "sys_role",
            "sys_dept",
            "sys_user",
        ):
            assert any(table in s and "FOR " in s for s in sql[:first_insert]), table
        async with plain() as db:
            assert await epoch(db) == 2
            assert (
                await db.scalar(
                    select(User.user_name).where(User.user_id == result["id"])
                )
                == "candidate"
            )
    finally:
        event.remove(plain.kw["bind"].sync_engine, "before_cursor_execute", record)


@pytest.mark.parametrize(
    "operation,payload",
    [
        (
            "role.create",
            RoleCreate(
                role_name="Candidate",
                role_code="R_CANDIDATE",
                data_scope="5",
                status="1",
            ),
        ),
        ("dept.create", DeptCreate(dept_name="Candidate", status="1")),
    ],
)
async def test_role_and_department_create_then_noop(
    system_sessions, operation, payload
):
    factory, plain = system_sessions
    async with factory() as db:
        result = await run(db, operation, payload)
        await db.commit()
    update_payload = (
        RoleUpdate(role_name="Candidate")
        if operation.startswith("role")
        else DeptUpdate(dept_name="Candidate")
    )
    async with factory() as db:
        await run(
            db, operation.replace("create", "update"), update_payload, result["id"]
        )
        await db.commit()
    async with plain() as db:
        assert await epoch(db) == 2


async def test_missing_permission_cross_tenant_and_input_limit(system_sessions):
    factory, _ = system_sessions
    async with factory() as db:
        with pytest.raises(BusinessException):
            await run(
                db,
                "user.update",
                UserUpdate(user_name="planner", nickname="outside", status="1"),
                102,
            )
        await db.rollback()
    async with factory() as db:
        with pytest.raises(BusinessException) as error:
            await run(db, "user.roles", [301] * 257, 101)
        assert error.value.error_code == "AUTHORIZATION_PLAN_LIMIT_EXCEEDED"
        await db.rollback()


async def test_department_preview_does_not_lock_setting(system_sessions):
    factory, plain = system_sessions
    async with plain() as setup:
        await setup.execute(
            insert(User).values(
                tenant_id=0,
                user_id=501,
                user_name="member",
                hashed_password="not-a-credential",
                status="1",
            )
        )
        await setup.execute(
            insert(Dept).values(
                tenant_id=0, dept_id=401, dept_name="Team", ancestors="0", status="1"
            )
        )
        await setup.commit()
    async with factory() as db:
        await user_department_assignment_service.preview_departments(
            db,
            actor_user_id=101,
            target_user_id=501,
            dept_assignments=[(401, True)],
            tenant=TENANT,
        )
        assert db._authorization_journal.unsafe is False
        await db.rollback()
