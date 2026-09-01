"""Phase 2-B3 department-centered membership policy tests."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.constants import (
    DATA_SCOPE_ALL,
    DATA_SCOPE_CUSTOM,
    DATA_SCOPE_SELF,
    IS_PRIMARY_NO,
    IS_PRIMARY_YES,
    STATUS_DISABLED,
    STATUS_ENABLED,
    SUPER_ADMIN_ROLE_CODE,
)
from app.core.exceptions import AuthorizationException, BusinessRuleException
from app.core.id_generator import next_id
from app.db.base import user_depts
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service.authorization_lock import (
    AuthorizationLockSet,
    authorization_lock_service,
)
from app.modules.system.service.grant_authority import grant_authority_service
from app.modules.system.service.tenant_association_writer import (
    replace_role_depts,
    replace_role_menus,
    replace_user_roles,
)
from app.modules.system.service.user_department_assignment_service import (
    user_department_assignment_service,
)
from tests.tenant_helpers import tenant_context


def _menu(permission: str) -> Menu:
    marker = next_id()
    return Menu(
        tenant_id=0,
        menu_id=marker,
        menu_name=f"phase2-membership-menu-{marker}",
        menu_type="F",
        permission=permission,
        status=STATUS_ENABLED,
    )


def _role(code: str, *, data_scope: str = DATA_SCOPE_SELF) -> Role:
    marker = next_id()
    return Role(
        tenant_id=0,
        role_id=marker,
        role_name=f"phase2-membership-role-{marker}",
        role_code=code,
        data_scope=data_scope,
        status=STATUS_ENABLED,
    )


def _dept(name: str) -> Dept:
    marker = next_id()
    return Dept(
        tenant_id=0,
        dept_id=marker,
        dept_name=f"{name}-{marker}",
        ancestors="0",
        order_num=0,
        status=STATUS_ENABLED,
    )


def _user(name: str, roles: list[Role], *, status: str = STATUS_ENABLED) -> User:
    return User(
        tenant_id=0,
        user_id=next_id(),
        user_name=f"{name}-{next_id()}",
        nickname=name,
        hashed_password="x",
        status=status,
        roles=roles,
    )


async def _bind(
    db: AsyncSession,
    user: User,
    dept: Dept,
    *,
    primary: bool,
) -> None:
    await db.execute(
        insert(user_depts).values(
            tenant_id=0,
            user_id=user.user_id,
            dept_id=dept.dept_id,
            is_primary=IS_PRIMARY_YES if primary else IS_PRIMARY_NO,
        )
    )


async def _member_ids(db: AsyncSession, dept: Dept) -> set[int]:
    return set(
        (
            await db.execute(
                select(user_depts.c.user_id).where(
                    user_depts.c.tenant_id == 0,
                    user_depts.c.dept_id == dept.dept_id,
                )
            )
        ).scalars()
    )


async def _persist_graph(db: AsyncSession, *objects: object) -> None:
    users = [value for value in objects if isinstance(value, User)]
    roles = [value for value in objects if isinstance(value, Role)]
    user_links = [(user, list(user.roles)) for user in users]
    role_menu_links = [(role, list(role.menus)) for role in roles]
    role_dept_links = [(role, list(role.depts)) for role in roles]
    related = [
        *[item for _owner, items in user_links for item in items],
        *[item for _owner, items in role_menu_links for item in items],
        *[item for _owner, items in role_dept_links for item in items],
    ]
    for user, _items in user_links:
        set_committed_value(user, "roles", [])
    for role, _items in role_menu_links:
        set_committed_value(role, "menus", [])
    for role, _items in role_dept_links:
        set_committed_value(role, "depts", [])
    db.add_all([*objects, *related])
    await db.flush()
    tenant = tenant_context(tenant_id=0)
    for user, linked_roles in user_links:
        await replace_user_roles(db, user, linked_roles, tenant=tenant)
    for role, menus in role_menu_links:
        await replace_role_menus(db, role, menus, tenant=tenant)
    for role, depts in role_dept_links:
        await replace_role_depts(db, role, depts, tenant=tenant)
    await db.flush()


async def test_member_candidates_fail_closed_when_an_old_member_is_hidden(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dept = _dept("phase2-membership")
    actor_role = _role(
        f"R_MEMBERSHIP_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_CUSTOM,
    )
    actor_role.menus = [
        _menu("system:dept:list"),
        _menu("system:dept:edit"),
        _menu("system:user:list"),
    ]
    actor_role.depts = [dept]
    target_role = _role(f"R_MEMBERSHIP_TARGET_{next_id()}")
    actor = _user("phase2-membership-actor", [actor_role])
    visible = _user("phase2-membership-visible", [target_role])
    hidden = _user("phase2-membership-hidden", [target_role])
    await _persist_graph(
        db_session, dept, actor_role, target_role, actor, visible, hidden
    )
    await _bind(db_session, actor, dept, primary=True)
    await _bind(db_session, visible, dept, primary=False)
    await _bind(db_session, hidden, dept, primary=False)

    authority = await grant_authority_service.build(
        db_session,
        actor.user_id,
        tenant=tenant_context(tenant_id=0, actor_user_id=actor.user_id),
    )
    scoped_authority = replace(
        authority,
        accessible_user_scope=frozenset({actor.user_id, visible.user_id}),
    )
    monkeypatch.setattr(
        grant_authority_service,
        "build",
        AsyncMock(return_value=scoped_authority),
    )

    with pytest.raises(AuthorizationException) as exc_info:
        await user_department_assignment_service.list_department_members(
            db_session,
            actor_user_id=actor.user_id,
            dept_id=dept.dept_id,
            query=None,
            current=1,
            size=20,
            tenant=tenant_context(tenant_id=0, actor_user_id=actor.user_id),
        )

    assert exc_info.value.error_code == "DEPT_MEMBERSHIP_GLOBAL_IMPACT_OUT_OF_SCOPE"


async def test_member_replacement_rejects_primary_removal_before_any_write(
    db_session: AsyncSession,
) -> None:
    super_role = await db_session.scalar(
        select(Role).where(
            Role.tenant_id == 0,
            Role.role_code == SUPER_ADMIN_ROLE_CODE,
        )
    )
    assert super_role is not None
    target_role = _role(f"R_MEMBERSHIP_TARGET_{next_id()}")
    dept = _dept("phase2-membership")
    actor = _user("phase2-membership-super", [super_role])
    primary_member = _user("phase2-membership-primary", [target_role])
    candidate = _user("phase2-membership-candidate", [target_role])
    await _persist_graph(
        db_session, target_role, dept, actor, primary_member, candidate
    )
    await _bind(db_session, primary_member, dept, primary=True)

    with pytest.raises(BusinessRuleException) as exc_info:
        await user_department_assignment_service.replace_department_members(
            db_session,
            actor_user_id=actor.user_id,
            dept_id=dept.dept_id,
            user_ids=[str(candidate.user_id)],
            tenant=tenant_context(tenant_id=0, actor_user_id=actor.user_id),
        )

    assert exc_info.value.error_code == "USER_PRIMARY_DEPT_REASSIGN_REQUIRED"
    assert await _member_ids(db_session, dept) == {primary_member.user_id}


async def test_member_replacement_adds_and_removes_complete_safe_set(
    db_session: AsyncSession,
) -> None:
    super_role = await db_session.scalar(
        select(Role).where(
            Role.tenant_id == 0,
            Role.role_code == SUPER_ADMIN_ROLE_CODE,
        )
    )
    assert super_role is not None
    target_role = _role(f"R_MEMBERSHIP_TARGET_{next_id()}")
    dept = _dept("phase2-membership")
    other_dept = _dept("phase2-membership-other")
    actor = _user("phase2-membership-super", [super_role])
    old_member = _user("phase2-membership-old", [target_role])
    candidate = _user("phase2-membership-candidate", [target_role])
    disabled_candidate = _user(
        "phase2-membership-disabled",
        [target_role],
        status=STATUS_DISABLED,
    )
    await _persist_graph(
        db_session,
        target_role,
        dept,
        other_dept,
        actor,
        old_member,
        candidate,
        disabled_candidate,
    )
    await _bind(db_session, old_member, other_dept, primary=True)
    await _bind(db_session, old_member, dept, primary=False)
    await _bind(db_session, candidate, other_dept, primary=True)

    result = await user_department_assignment_service.replace_department_members(
        db_session,
        actor_user_id=actor.user_id,
        dept_id=dept.dept_id,
        user_ids=[str(candidate.user_id)],
        tenant=tenant_context(tenant_id=0, actor_user_id=actor.user_id),
    )

    assert result.added == 1
    assert result.removed == 1
    assert await _member_ids(db_session, dept) == {candidate.user_id}

    with pytest.raises(BusinessRuleException) as exc_info:
        await user_department_assignment_service.replace_department_members(
            db_session,
            actor_user_id=actor.user_id,
            dept_id=dept.dept_id,
            user_ids=[str(disabled_candidate.user_id)],
            tenant=tenant_context(tenant_id=0, actor_user_id=actor.user_id),
        )
    assert exc_info.value.error_code == "USER_DEPT_MEMBER_NOT_AVAILABLE"


async def test_member_replacement_rejects_role_definition_drift_after_preload(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    super_role = await db_session.scalar(
        select(Role).where(
            Role.tenant_id == 0,
            Role.role_code == SUPER_ADMIN_ROLE_CODE,
        )
    )
    assert super_role is not None
    target_role = _role(f"R_MEMBERSHIP_DRIFT_{next_id()}")
    dept = _dept("phase2-membership-drift")
    other_dept = _dept("phase2-membership-drift-other")
    actor = _user("drift-super", [super_role])
    candidate = _user("drift-candidate", [target_role])
    await _persist_graph(db_session, target_role, dept, other_dept, actor, candidate)
    await _bind(db_session, candidate, other_dept, primary=True)

    async def mutate_role_after_preload(
        _db: AsyncSession,
        *,
        role_ids: set[int],
        dept_ids: set[int],
        user_ids: set[int],
        tenant: object,
    ) -> AuthorizationLockSet:
        del tenant
        target_role.data_scope = DATA_SCOPE_ALL
        await db_session.flush()
        return AuthorizationLockSet(
            role_ids=tuple(sorted(role_ids)),
            dept_ids=tuple(sorted(dept_ids)),
            user_ids=tuple(sorted(user_ids)),
        )

    monkeypatch.setattr(
        authorization_lock_service,
        "lock_targets",
        mutate_role_after_preload,
    )

    with pytest.raises(BusinessRuleException) as exc_info:
        await user_department_assignment_service.replace_department_members(
            db_session,
            actor_user_id=actor.user_id,
            dept_id=dept.dept_id,
            user_ids=[str(candidate.user_id)],
            tenant=tenant_context(tenant_id=0, actor_user_id=actor.user_id),
        )

    assert exc_info.value.error_code == "AUTHORIZATION_SNAPSHOT_STALE"
    assert await _member_ids(db_session, dept) == set()
