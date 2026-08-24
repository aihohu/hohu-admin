"""Phase 3 delegated Role policy and global-impact tests."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import (
    DATA_SCOPE_ALL,
    DATA_SCOPE_SELF,
    STATUS_ENABLED,
    SUPER_ADMIN_ROLE_CODE,
)
from app.core.exceptions import AuthorizationException, BusinessRuleException
from app.core.id_generator import next_id
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.schemas.role import RoleCreate, RoleUpdate
from app.modules.system.service.authorization_lock import authorization_lock_service
from app.modules.system.service.role_management_service import role_management_service


def _menu(permission: str, *, parent_id: int | None = None) -> Menu:
    marker = next_id()
    return Menu(
        menu_id=marker,
        parent_id=parent_id,
        menu_name=f"phase3-role-menu-{marker}",
        menu_type="F",
        permission=permission,
        status=STATUS_ENABLED,
    )


def _role(
    code: str,
    *,
    data_scope: str = DATA_SCOPE_SELF,
    menus: list[Menu] | None = None,
) -> Role:
    marker = next_id()
    role = Role(
        role_id=marker,
        role_name=f"phase3-role-{marker}",
        role_code=code,
        data_scope=data_scope,
        status=STATUS_ENABLED,
    )
    role.menus = menus or []
    return role


def _user(name: str, roles: list[Role]) -> User:
    return User(
        user_id=next_id(),
        user_name=name,
        nickname=name,
        hashed_password="x",
        status=STATUS_ENABLED,
        roles=roles,
    )


async def _actor(
    db: AsyncSession,
    permission: str,
    *,
    data_scope: str = DATA_SCOPE_ALL,
    extra_menus: list[Menu] | None = None,
) -> User:
    permission_menu = _menu(permission)
    role = _role(
        f"R_PHASE3_ACTOR_{next_id()}",
        data_scope=data_scope,
        menus=[permission_menu, *(extra_menus or [])],
    )
    actor = _user(f"phase3-role-actor-{next_id()}", [role])
    db.add(actor)
    await db.flush()
    return actor


async def test_delegated_actor_can_create_role_below_authority_ceiling(
    db_session: AsyncSession,
) -> None:
    actor = await _actor(db_session, "system:role:add")

    created = await role_management_service.create(
        db_session,
        RoleCreate(
            role_name=f"Scoped auditor {next_id()}",
            role_code=f"R_SCOPED_AUDITOR_{next_id()}",
            data_scope=DATA_SCOPE_SELF,
            status=STATUS_ENABLED,
        ),
        actor_user_id=actor.user_id,
    )

    assert created.role_id is not None
    assert created.data_scope == DATA_SCOPE_SELF


async def test_role_create_rejects_latent_custom_departments_for_self_scope(
    db_session: AsyncSession,
) -> None:
    actor = await _actor(db_session, "system:role:add")
    hidden_dept = Dept(
        dept_id=next_id(),
        parent_id=None,
        ancestors="0",
        dept_name=f"phase3-role-hidden-{next_id()}",
        order_num=0,
        status=STATUS_ENABLED,
    )
    db_session.add(hidden_dept)
    await db_session.flush()

    with pytest.raises(BusinessRuleException) as exc_info:
        await role_management_service.create(
            db_session,
            RoleCreate(
                role_name=f"Self with latent depts {next_id()}",
                role_code=f"R_SELF_LATENT_{next_id()}",
                data_scope=DATA_SCOPE_SELF,
                status=STATUS_ENABLED,
                dept_ids=[hidden_dept.dept_id],
            ),
            actor_user_id=actor.user_id,
        )

    assert exc_info.value.error_code == "ROLE_DEPTS_REQUIRE_CUSTOM_SCOPE"


async def test_role_update_rejects_latent_custom_departments_for_self_scope(
    db_session: AsyncSession,
) -> None:
    actor = await _actor(db_session, "system:role:edit")
    target = _role(f"R_PHASE3_NO_LATENT_{next_id()}")
    hidden_dept = Dept(
        dept_id=next_id(),
        parent_id=None,
        ancestors="0",
        dept_name=f"phase3-role-update-hidden-{next_id()}",
        order_num=0,
        status=STATUS_ENABLED,
    )
    db_session.add_all([target, hidden_dept])
    await db_session.flush()

    with pytest.raises(BusinessRuleException) as exc_info:
        await role_management_service.update(
            db_session,
            target.role_id,
            RoleUpdate(dept_ids=[hidden_dept.dept_id]),
            actor_user_id=actor.user_id,
        )

    assert exc_info.value.error_code == "ROLE_DEPTS_REQUIRE_CUSTOM_SCOPE"


async def test_role_update_rejects_member_outside_actor_user_scope(
    db_session: AsyncSession,
) -> None:
    actor = await _actor(
        db_session,
        "system:role:edit",
        data_scope=DATA_SCOPE_SELF,
    )
    target = _role(f"R_PHASE3_TARGET_{next_id()}")
    member = _user(f"phase3-role-member-{next_id()}", [target])
    db_session.add(member)
    await db_session.flush()

    with pytest.raises(AuthorizationException) as exc_info:
        await role_management_service.update(
            db_session,
            target.role_id,
            RoleUpdate(role_name=f"Updated {next_id()}"),
            actor_user_id=actor.user_id,
        )

    assert exc_info.value.error_code == "AI_ROLE_GLOBAL_IMPACT_OUT_OF_SCOPE"


async def test_role_update_rejects_self_membership(
    db_session: AsyncSession,
) -> None:
    edit_menu = _menu("system:role:edit")
    actor_role = _role(
        f"R_PHASE3_SELF_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[edit_menu],
    )
    target = _role(f"R_PHASE3_SELF_TARGET_{next_id()}")
    actor = _user(f"phase3-role-self-{next_id()}", [actor_role, target])
    db_session.add(actor)
    await db_session.flush()

    with pytest.raises(AuthorizationException) as exc_info:
        await role_management_service.update(
            db_session,
            target.role_id,
            RoleUpdate(status="2"),
            actor_user_id=actor.user_id,
        )

    assert exc_info.value.error_code == "AI_ROLE_SELF_MUTATION_FORBIDDEN"


async def test_role_menu_replacement_derives_ancestors_and_uses_global_lock_order(
    db_session: AsyncSession,
) -> None:
    parent = _menu("system:phase3:parent")
    child = _menu("system:phase3:child", parent_id=parent.menu_id)
    actor = await _actor(
        db_session,
        "system:role:menu-auth",
        extra_menus=[parent, child],
    )
    target = _role(f"R_PHASE3_MENU_TARGET_{next_id()}")
    db_session.add(target)
    await db_session.flush()

    with patch.object(
        authorization_lock_service,
        "lock_targets",
        new=AsyncMock(),
    ) as lock_targets:
        updated = await role_management_service.update_menus(
            db_session,
            target.role_id,
            [child.menu_id],
            actor_user_id=actor.user_id,
        )

    assert {menu.menu_id for menu in updated.menus} == {parent.menu_id, child.menu_id}
    lock_kwargs = lock_targets.await_args.kwargs
    assert lock_kwargs["role_ids"] == tuple(sorted(lock_kwargs["role_ids"]))
    assert lock_kwargs["dept_ids"] == tuple(sorted(lock_kwargs["dept_ids"]))
    assert lock_kwargs["user_ids"] == tuple(sorted(lock_kwargs["user_ids"]))
    assert target.role_id in lock_kwargs["role_ids"]
    assert actor.user_id in lock_kwargs["user_ids"]


async def test_role_update_locks_materialized_descendant_departments(
    db_session: AsyncSession,
) -> None:
    root = Dept(
        dept_id=next_id(),
        parent_id=None,
        ancestors="0",
        dept_name=f"phase3-role-lock-root-{next_id()}",
        order_num=0,
        status=STATUS_ENABLED,
    )
    child = Dept(
        dept_id=next_id(),
        parent_id=root.dept_id,
        ancestors=f"0,{root.dept_id}",
        dept_name=f"phase3-role-lock-child-{next_id()}",
        order_num=0,
        status=STATUS_ENABLED,
    )
    actor = await _actor(db_session, "system:role:edit")
    target = _role(
        f"R_PHASE3_DESCENDANT_LOCK_{next_id()}",
        data_scope="4",
    )
    member = _user(f"phase3-role-lock-member-{next_id()}", [target])
    member.depts = [root]
    db_session.add_all([root, child, target, member])
    await db_session.flush()

    with patch.object(
        authorization_lock_service,
        "lock_targets",
        new=AsyncMock(),
    ) as lock_targets:
        await role_management_service.update(
            db_session,
            target.role_id,
            RoleUpdate(role_name=f"Updated {next_id()}"),
            actor_user_id=actor.user_id,
        )

    assert {root.dept_id, child.dept_id} <= set(
        lock_targets.await_args.kwargs["dept_ids"]
    )


async def test_role_execute_rejects_approved_snapshot_drift(
    db_session: AsyncSession,
) -> None:
    actor = await _actor(db_session, "system:role:edit")
    target = _role(f"R_PHASE3_STALE_TARGET_{next_id()}")
    db_session.add(target)
    await db_session.flush()
    request = RoleUpdate(role_name=f"Approved name {next_id()}")
    preview = await role_management_service.preview_update(
        db_session,
        target.role_id,
        request,
        actor_user_id=actor.user_id,
    )
    target.role_desc = "concurrent change"
    await db_session.flush()

    with pytest.raises(BusinessRuleException) as exc_info:
        await role_management_service.update(
            db_session,
            target.role_id,
            request,
            actor_user_id=actor.user_id,
            expected_snapshot=preview.snapshot,
        )

    assert exc_info.value.error_code == "AI_PREPARED_ACTION_SNAPSHOT_STALE"


async def test_role_summary_blocks_target_with_protected_member(
    db_session: AsyncSession,
) -> None:
    actor = await _actor(db_session, "system:role:list")
    target = _role(f"R_PHASE3_PROTECTED_MEMBER_{next_id()}")
    super_role = await db_session.scalar(
        select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
    )
    assert super_role is not None
    member = _user(f"phase3-role-protected-{next_id()}", [target, super_role])
    db_session.add(member)
    await db_session.flush()

    summaries, total, _contributors = await role_management_service.summarize_roles(
        db_session,
        actor_user_id=actor.user_id,
        query=target.role_code,
    )

    assert total == 1
    assert summaries[0].delegable is False
    assert summaries[0].blocked_reason_code == "AI_ROLE_GLOBAL_IMPACT_OUT_OF_SCOPE"
