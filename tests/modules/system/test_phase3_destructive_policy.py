"""Phase 3 super-admin destructive boundaries for Dept and Role aggregates."""

import inspect

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import (
    DATA_SCOPE_ALL,
    STATUS_ENABLED,
    SUPER_ADMIN_ROLE_CODE,
)
from app.core.exceptions import BusinessException
from app.core.id_generator import next_id
from app.db.base import role_depts, user_roles
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service.dept_service import dept_service
from app.modules.system.service.role_service import role_service


def _role(code: str) -> Role:
    marker = next_id()
    return Role(
        role_id=marker,
        role_name=f"phase3-delete-role-{marker}",
        role_code=code,
        data_scope=DATA_SCOPE_ALL,
        status=STATUS_ENABLED,
    )


def _user(name: str, role: Role) -> User:
    marker = next_id()
    return User(
        user_id=marker,
        user_name=f"{name}-{marker}",
        nickname=name,
        hashed_password="x",
        status=STATUS_ENABLED,
        roles=[role],
    )


def _dept(name: str) -> Dept:
    marker = next_id()
    return Dept(
        dept_id=marker,
        parent_id=None,
        ancestors="0",
        dept_name=f"{name}-{marker}",
        order_num=0,
        status=STATUS_ENABLED,
    )


async def _super_actor(db: AsyncSession, name: str) -> User:
    super_role = await db.scalar(
        select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
    )
    assert super_role is not None
    actor = _user(name, super_role)
    db.add(actor)
    await db.flush()
    return actor


def test_destructive_services_require_actor_context() -> None:
    for method in (
        dept_service.delete,
        dept_service.batch_delete,
        role_service.delete_role,
        role_service.batch_delete_roles,
    ):
        assert "actor_user_id" in inspect.signature(method).parameters
        assert ".commit(" not in inspect.getsource(method)


async def test_normal_admin_cannot_delete_department_or_role(
    db_session: AsyncSession,
) -> None:
    normal_role = _role(f"R_PHASE3_NORMAL_{next_id()}")
    normal_role.menus = [
        Menu(
            menu_id=next_id(),
            menu_name=f"phase3-destructive-{permission}",
            menu_type="F",
            permission=permission,
            status=STATUS_ENABLED,
        )
        for permission in (
            "system:dept:delete",
            "system:dept:batch-delete",
            "system:role:delete",
            "system:role:batch-delete",
        )
    ]
    actor = _user("phase3-delete-normal", normal_role)
    department = _dept("phase3-delete-normal")
    target_role = _role(f"R_PHASE3_TARGET_{next_id()}")
    db_session.add_all([normal_role, actor, department, target_role])
    await db_session.flush()

    with pytest.raises(BusinessException):
        await dept_service.delete(
            db_session,
            department.dept_id,
            actor_user_id=actor.user_id,
        )
    with pytest.raises(BusinessException):
        await role_service.delete_role(
            db_session,
            target_role.role_id,
            actor_user_id=actor.user_id,
        )

    assert await db_session.get(Dept, department.dept_id) is department
    assert await db_session.get(Role, target_role.role_id) is target_role


async def test_department_delete_rejects_role_scope_reference(
    db_session: AsyncSession,
) -> None:
    actor = await _super_actor(db_session, "phase3-delete-super")
    target = _dept("phase3-delete-referenced")
    referenced_by = _role(f"R_PHASE3_REFERENCE_{next_id()}")
    db_session.add_all([target, referenced_by])
    await db_session.flush()
    await db_session.execute(
        insert(role_depts).values(
            role_id=referenced_by.role_id,
            dept_id=target.dept_id,
        )
    )

    with pytest.raises(BusinessException) as exc_info:
        await dept_service.delete(
            db_session,
            target.dept_id,
            actor_user_id=actor.user_id,
        )

    assert exc_info.value.error_code == "DEPT_DELETE_REFERENCED"
    assert await db_session.get(Dept, target.dept_id) is target


async def test_role_delete_rejects_member_and_batch_is_atomic(
    db_session: AsyncSession,
) -> None:
    actor = await _super_actor(db_session, "phase3-role-delete-super")
    referenced = _role(f"R_PHASE3_MEMBER_{next_id()}")
    clean = _role(f"R_PHASE3_CLEAN_{next_id()}")
    member = _user("phase3-role-member", referenced)
    db_session.add_all([referenced, clean, member])
    await db_session.flush()

    with pytest.raises(BusinessException) as exc_info:
        await role_service.batch_delete_roles(
            db_session,
            [clean.role_id, referenced.role_id, clean.role_id],
            actor_user_id=actor.user_id,
        )

    assert exc_info.value.error_code == "ROLE_DELETE_REFERENCED"
    assert (
        await db_session.scalar(
            select(Role.role_id).where(Role.role_id == clean.role_id)
        )
        == clean.role_id
    )
    assert (
        await db_session.scalar(
            select(user_roles.c.user_id).where(
                user_roles.c.role_id == referenced.role_id
            )
        )
        == member.user_id
    )


async def test_role_delete_removes_agent_binding_before_aggregate(
    db_session: AsyncSession,
) -> None:
    actor = await _super_actor(db_session, "p3-binding-super")
    target = _role(f"R_PHASE3_BINDING_{next_id()}")
    agent = AiAgent(
        agent_id=next_id(),
        code=f"phase3-delete-agent-{next_id()}",
        name="Phase 3 delete agent",
        description="Phase 3 delete reference test Agent with sufficient detail.",
        enabled=True,
        is_builtin=False,
        display_order=0,
        system_prompt="",
        risk_appetite="balanced",
    )
    db_session.add_all([target, agent])
    await db_session.flush()
    db_session.add(
        RoleAiAgent(
            role_id=target.role_id,
            agent_id=agent.agent_id,
            enabled=True,
        )
    )
    await db_session.flush()

    deleted = await role_service.batch_delete_roles(
        db_session,
        [target.role_id],
        actor_user_id=actor.user_id,
    )

    assert deleted == 1
    assert await db_session.get(Role, target.role_id) is None
    assert (
        await db_session.scalar(
            select(RoleAiAgent.role_id).where(RoleAiAgent.role_id == target.role_id)
        )
        is None
    )


@pytest.mark.parametrize(
    ("kind", "wrong_permission"),
    [
        ("dept", "system:dept:batch-delete"),
        ("role", "system:role:batch-delete"),
    ],
)
async def test_super_admin_single_delete_requires_the_original_permission(
    db_session: AsyncSession,
    kind: str,
    wrong_permission: str,
) -> None:
    actor = await _super_actor(db_session, f"phase3-exact-{kind}")
    role = next(
        value for value in actor.roles if value.role_code == SUPER_ADMIN_ROLE_CODE
    )
    wrong_menu = await db_session.scalar(
        select(Menu).where(Menu.permission == wrong_permission)
    )
    assert wrong_menu is not None
    role.menus = [wrong_menu]
    if kind == "dept":
        target = _dept("phase3-exact-permission")
        db_session.add(target)
        await db_session.flush()
        operation = dept_service.delete(
            db_session,
            target.dept_id,
            actor_user_id=actor.user_id,
        )
    else:
        target = _role(f"R_PHASE3_EXACT_{next_id()}")
        db_session.add(target)
        await db_session.flush()
        operation = role_service.delete_role(
            db_session,
            target.role_id,
            actor_user_id=actor.user_id,
        )

    with pytest.raises(BusinessException) as exc_info:
        await operation

    assert exc_info.value.error_code == "MISSING_PERMISSION"


@pytest.mark.parametrize(
    ("kind", "permission"),
    [
        ("dept", "system:dept:delete"),
        ("role", "system:role:delete"),
    ],
)
async def test_super_admin_single_delete_accepts_the_exact_original_permission(
    db_session: AsyncSession,
    kind: str,
    permission: str,
) -> None:
    actor = await _super_actor(db_session, f"phase3-exact-ok-{kind}")
    role = next(
        value for value in actor.roles if value.role_code == SUPER_ADMIN_ROLE_CODE
    )
    permission_menu = await db_session.scalar(
        select(Menu).where(Menu.permission == permission)
    )
    assert permission_menu is not None
    role.menus = [permission_menu]
    if kind == "dept":
        target = _dept("phase3-exact-ok")
        db_session.add(target)
        await db_session.flush()
        await dept_service.delete(
            db_session,
            target.dept_id,
            actor_user_id=actor.user_id,
        )
        assert await db_session.get(Dept, target.dept_id) is None
    else:
        target = _role(f"R_PHASE3_EXACT_OK_{next_id()}")
        db_session.add(target)
        await db_session.flush()
        await role_service.delete_role(
            db_session,
            target.role_id,
            actor_user_id=actor.user_id,
        )
        assert await db_session.get(Role, target.role_id) is None


@pytest.mark.parametrize(
    ("kind", "single_permission"),
    [
        ("dept", "system:dept:delete"),
        ("role", "system:role:delete"),
    ],
)
async def test_super_admin_batch_delete_rejects_the_single_delete_permission(
    db_session: AsyncSession,
    kind: str,
    single_permission: str,
) -> None:
    actor = await _super_actor(db_session, f"phase3-batch-exact-{kind}")
    role = next(
        value for value in actor.roles if value.role_code == SUPER_ADMIN_ROLE_CODE
    )
    permission_menu = await db_session.scalar(
        select(Menu).where(Menu.permission == single_permission)
    )
    assert permission_menu is not None
    role.menus = [permission_menu]
    if kind == "dept":
        target = _dept("phase3-batch-exact")
        db_session.add(target)
        await db_session.flush()
        operation = dept_service.batch_delete(
            db_session,
            [target.dept_id],
            actor_user_id=actor.user_id,
        )
    else:
        target = _role(f"R_PHASE3_BATCH_EXACT_{next_id()}")
        db_session.add(target)
        await db_session.flush()
        operation = role_service.batch_delete_roles(
            db_session,
            [target.role_id],
            actor_user_id=actor.user_id,
        )

    with pytest.raises(BusinessException) as exc_info:
        await operation

    assert exc_info.value.error_code == "MISSING_PERMISSION"
