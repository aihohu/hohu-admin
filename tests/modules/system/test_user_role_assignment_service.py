"""Phase 2-B shared user role-assignment policy tests."""

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.constants import (
    DATA_SCOPE_ALL,
    DATA_SCOPE_CUSTOM,
    DATA_SCOPE_SELF,
    STATUS_ENABLED,
    SUPER_ADMIN_ROLE_CODE,
    USER_ROLE_CODE,
)
from app.core.exceptions import AuthorizationException, BusinessRuleException
from app.core.id_generator import next_id
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.system.constants import USER_ROLE_AUTH_PERMISSION
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service.user_role_assignment_service import (
    user_role_assignment_service,
)

USER_ADD_PERMISSION = "system:user:add"
USER_EDIT_PERMISSION = "system:user:edit"
USER_IMPORT_PERMISSION = "system:user:import"


def _menu(permission: str) -> Menu:
    marker = next_id()
    return Menu(
        menu_id=marker,
        menu_name=f"phase2-menu-{marker}",
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
        role_name=f"phase2-role-{marker}",
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


async def _reload_user(db: AsyncSession, user_id: int) -> User:
    return (
        await db.execute(
            select(User)
            .where(User.user_id == user_id)
            .options(selectinload(User.roles))
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def test_replace_roles_accepts_a_dominated_complete_set(
    db_session: AsyncSession,
) -> None:
    delegated_permission = _menu(f"qa:user-role:{next_id()}:read")
    actor_role = _role(
        f"R_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[
            _menu(USER_EDIT_PERMISSION),
            _menu(USER_ROLE_AUTH_PERMISSION),
            delegated_permission,
        ],
    )
    old_role = _role(f"R_OLD_{next_id()}")
    new_role = _role(
        f"R_NEW_{next_id()}",
        menus=[delegated_permission],
    )
    actor = _user(f"phase2-actor-{next_id()}", [actor_role])
    target = _user(f"phase2-target-{next_id()}", [old_role])
    db_session.add_all(
        [delegated_permission, actor_role, old_role, new_role, actor, target]
    )
    await db_session.flush()

    result = await user_role_assignment_service.replace_roles(
        db_session,
        actor_user_id=actor.user_id,
        target_user_id=target.user_id,
        role_ids=[new_role.role_id],
    )
    await db_session.flush()

    reloaded = await _reload_user(db_session, target.user_id)
    assert result.old_role_ids == (old_role.role_id,)
    assert result.new_role_ids == (new_role.role_id,)
    assert [role.role_id for role in reloaded.roles] == [new_role.role_id]


async def test_replace_roles_rejects_new_or_removed_authority_above_actor(
    db_session: AsyncSession,
) -> None:
    outside_permission = _menu(f"qa:user-role:{next_id()}:outside")
    actor_role = _role(
        f"R_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[_menu(USER_EDIT_PERMISSION), _menu(USER_ROLE_AUTH_PERMISSION)],
    )
    safe_role = _role(f"R_SAFE_{next_id()}")
    elevated_role = _role(
        f"R_ELEVATED_{next_id()}",
        menus=[outside_permission],
    )
    actor = _user(f"phase2-actor-{next_id()}", [actor_role])
    target = _user(f"phase2-target-{next_id()}", [safe_role])
    db_session.add_all(
        [outside_permission, actor_role, safe_role, elevated_role, actor, target]
    )
    await db_session.flush()

    with pytest.raises(AuthorizationException) as new_exc:
        await user_role_assignment_service.replace_roles(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            role_ids=[elevated_role.role_id],
        )
    assert new_exc.value.error_code == "USER_ROLE_AUTHORITY_EXCEEDED"

    target.roles = [elevated_role]
    await db_session.flush()
    with pytest.raises(AuthorizationException) as old_exc:
        await user_role_assignment_service.replace_roles(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            role_ids=[safe_role.role_id],
        )
    assert old_exc.value.error_code == "USER_ROLE_AUTHORITY_EXCEEDED"


async def test_replace_roles_rejects_self_and_super_admin_targets(
    db_session: AsyncSession,
) -> None:
    actor_role = _role(
        f"R_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[_menu(USER_EDIT_PERMISSION), _menu(USER_ROLE_AUTH_PERMISSION)],
    )
    safe_role = _role(f"R_SAFE_{next_id()}")
    super_role = await db_session.scalar(
        select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
    )
    assert super_role is not None
    actor = _user(f"phase2-actor-{next_id()}", [actor_role])
    protected_target = _user(f"phase2-super-{next_id()}", [super_role])
    db_session.add_all([actor_role, safe_role, actor, protected_target])
    await db_session.flush()

    with pytest.raises(BusinessRuleException) as self_exc:
        await user_role_assignment_service.replace_roles(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=actor.user_id,
            role_ids=[safe_role.role_id],
        )
    assert self_exc.value.error_code == "USER_ROLE_SELF_ASSIGNMENT_FORBIDDEN"

    with pytest.raises(AuthorizationException) as super_exc:
        await user_role_assignment_service.replace_roles(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=protected_target.user_id,
            role_ids=[safe_role.role_id],
        )
    assert super_exc.value.error_code == "USER_ROLE_SUPER_ADMIN_REQUIRED"


async def test_created_user_uses_fixed_default_role_without_role_auth(
    db_session: AsyncSession,
) -> None:
    default_role = await db_session.scalar(
        select(Role)
        .where(Role.role_code == USER_ROLE_CODE)
        .options(selectinload(Role.menus))
    )
    assert default_role is not None
    actor_role = _role(
        f"R_CREATOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[_menu(USER_ADD_PERMISSION), *default_role.menus],
    )
    actor = _user(f"phase2-creator-{next_id()}", [actor_role])
    target = _user(f"phase2-created-{next_id()}", [])
    db_session.add_all([actor_role, actor, target])
    await db_session.flush()

    result = await user_role_assignment_service.assign_created_user_roles(
        db_session,
        actor_user_id=actor.user_id,
        target_user_id=target.user_id,
        role_ids=None,
        dept_ids=[],
    )
    await db_session.flush()

    reloaded = await _reload_user(db_session, target.user_id)
    assert result.new_role_ids == (default_role.role_id,)
    assert [role.role_code for role in reloaded.roles] == [USER_ROLE_CODE]


async def test_created_user_default_role_still_requires_dominance(
    db_session: AsyncSession,
) -> None:
    actor_role = _role(
        f"R_NARROW_CREATOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[_menu(USER_ADD_PERMISSION)],
    )
    actor = _user(f"phase2-narrow-creator-{next_id()}", [actor_role])
    target = _user(f"phase2-created-{next_id()}", [])
    db_session.add_all([actor_role, actor, target])
    await db_session.flush()

    with pytest.raises(AuthorizationException) as exc_info:
        await user_role_assignment_service.assign_created_user_roles(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            role_ids=None,
            dept_ids=[],
        )

    assert exc_info.value.error_code == "USER_ROLE_AUTHORITY_EXCEEDED"


async def test_created_user_explicit_roles_require_role_auth_even_when_empty(
    db_session: AsyncSession,
) -> None:
    actor_role = _role(
        f"R_CREATOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[_menu(USER_ADD_PERMISSION)],
    )
    actor = _user(f"phase2-creator-{next_id()}", [actor_role])
    target = _user(f"phase2-created-{next_id()}", [])
    db_session.add_all([actor_role, actor, target])
    await db_session.flush()

    with pytest.raises(AuthorizationException) as exc_info:
        await user_role_assignment_service.assign_created_user_roles(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            role_ids=[],
            dept_ids=[],
        )

    assert exc_info.value.error_code == "MISSING_PERMISSION"


async def test_import_role_column_requires_role_auth_even_when_rows_are_empty(
    db_session: AsyncSession,
) -> None:
    actor_role = _role(
        f"R_IMPORTER_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[_menu(USER_IMPORT_PERMISSION)],
    )
    actor = _user(f"phase2-importer-{next_id()}", [actor_role])
    db_session.add_all([actor_role, actor])
    await db_session.flush()

    await user_role_assignment_service.ensure_import_permissions(
        db_session,
        actor_user_id=actor.user_id,
        has_role_column=False,
    )
    with pytest.raises(AuthorizationException) as exc_info:
        await user_role_assignment_service.ensure_import_permissions(
            db_session,
            actor_user_id=actor.user_id,
            has_role_column=True,
        )

    assert exc_info.value.error_code == "MISSING_PERMISSION"


@pytest.mark.parametrize("explicit_role", [False, True])
async def test_import_writer_revalidates_role_auth_inside_the_locked_policy(
    db_session: AsyncSession,
    explicit_role: bool,
) -> None:
    actor_role = _role(
        f"R_IMPORTER_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[_menu(USER_IMPORT_PERMISSION)],
    )
    delegated_role = _role(f"R_IMPORTED_{next_id()}")
    actor = _user(f"phase2-importer-{next_id()}", [actor_role])
    target = _user(f"phase2-imported-{next_id()}", [])
    db_session.add_all([actor_role, delegated_role, actor, target])
    await db_session.flush()

    with pytest.raises(AuthorizationException) as exc_info:
        await user_role_assignment_service.assign_imported_user_roles(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            role_ids=([delegated_role.role_id] if explicit_role else None),
            dept_ids=[],
            has_role_column=True,
        )

    assert exc_info.value.error_code == "MISSING_PERMISSION"


async def test_assignable_roles_returns_only_dominated_minimal_candidates(
    db_session: AsyncSession,
) -> None:
    delegated_permission = _menu(f"qa:assignable:{next_id()}:read")
    outside_permission = _menu(f"qa:assignable:{next_id()}:outside")
    actor_role = _role(
        f"R_CANDIDATE_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[_menu(USER_ROLE_AUTH_PERMISSION), delegated_permission],
    )
    assignable = _role(
        f"R_ASSIGNABLE_OK_{next_id()}",
        menus=[delegated_permission],
    )
    outside = _role(
        f"R_ASSIGNABLE_NO_{next_id()}",
        menus=[outside_permission],
    )
    actor = _user(f"phase2-assignable-{next_id()}", [actor_role])
    db_session.add_all(
        [
            delegated_permission,
            outside_permission,
            actor_role,
            assignable,
            outside,
            actor,
        ]
    )
    await db_session.flush()

    result = await user_role_assignment_service.list_assignable_roles(
        db_session,
        actor_user_id=actor.user_id,
        query="R_ASSIGNABLE_",
        limit=20,
    )

    assert [role.role_id for role in result] == [assignable.role_id]


async def test_replace_roles_rejects_out_of_scope_custom_role(
    db_session: AsyncSession,
) -> None:
    own_dept = Dept(
        dept_id=next_id(),
        dept_name=f"phase2-own-{next_id()}",
        ancestors="0",
        order_num=0,
        status=STATUS_ENABLED,
    )
    outside_dept = Dept(
        dept_id=next_id(),
        dept_name=f"phase2-outside-{next_id()}",
        ancestors="0",
        order_num=0,
        status=STATUS_ENABLED,
    )
    actor_role = _role(
        f"R_CUSTOM_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_CUSTOM,
        menus=[_menu(USER_EDIT_PERMISSION), _menu(USER_ROLE_AUTH_PERMISSION)],
    )
    actor_role.depts = [own_dept]
    old_role = _role(f"R_CUSTOM_OLD_{next_id()}")
    outside_role = _role(
        f"R_CUSTOM_OUTSIDE_{next_id()}",
        data_scope=DATA_SCOPE_CUSTOM,
    )
    outside_role.depts = [outside_dept]
    actor = _user(f"phase2-custom-actor-{next_id()}", [actor_role])
    actor.depts = [own_dept]
    target = _user(f"phase2-custom-target-{next_id()}", [old_role])
    target.depts = [own_dept]
    db_session.add_all(
        [own_dept, outside_dept, actor_role, old_role, outside_role, actor, target]
    )
    await db_session.flush()

    with pytest.raises(AuthorizationException) as exc_info:
        await user_role_assignment_service.replace_roles(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            role_ids=[outside_role.role_id],
        )

    assert exc_info.value.error_code == "USER_ROLE_AUTHORITY_EXCEEDED"


async def test_hypothetical_custom_scope_drops_subject_from_removed_department(
    db_session: AsyncSession,
) -> None:
    old_dept = Dept(
        dept_id=next_id(),
        dept_name=f"phase2-custom-old-{next_id()}",
        ancestors="0",
        order_num=0,
        status=STATUS_ENABLED,
    )
    new_dept = Dept(
        dept_id=next_id(),
        dept_name=f"phase2-custom-new-{next_id()}",
        ancestors="0",
        order_num=0,
        status=STATUS_ENABLED,
    )
    custom_role = _role(
        f"R_CUSTOM_TARGET_{next_id()}",
        data_scope=DATA_SCOPE_CUSTOM,
    )
    custom_role.depts = [old_dept]
    target = _user(f"phase2-custom-target-{next_id()}", [custom_role])
    target.depts = [old_dept]
    db_session.add_all([old_dept, new_dept, custom_role, target])
    await db_session.flush()

    authority = await user_role_assignment_service.materialize_role_set_authority(
        db_session,
        user=target,
        roles=[custom_role],
        depts=[new_dept],
    )

    assert authority.accessible_dept_ids == frozenset({old_dept.dept_id})
    assert authority.accessible_user_ids == frozenset()


async def test_replace_roles_rejects_agent_grant_above_actor(
    db_session: AsyncSession,
) -> None:
    actor_role = _role(
        f"R_AGENT_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[_menu(USER_EDIT_PERMISSION), _menu(USER_ROLE_AUTH_PERMISSION)],
    )
    old_role = _role(f"R_AGENT_OLD_{next_id()}")
    delegated_role = _role(f"R_AGENT_NEW_{next_id()}")
    agent = AiAgent(
        agent_id=next_id(),
        code=f"phase2-agent-{next_id()}",
        name="Phase 2 agent",
        description="Phase 2 agent",
        enabled=True,
    )
    actor = _user(f"phase2-agent-actor-{next_id()}", [actor_role])
    target = _user(f"phase2-agent-target-{next_id()}", [old_role])
    db_session.add_all([actor_role, old_role, delegated_role, agent, actor, target])
    await db_session.flush()
    db_session.add(
        RoleAiAgent(
            role_id=delegated_role.role_id,
            agent_id=agent.agent_id,
            enabled=True,
        )
    )
    await db_session.flush()

    with pytest.raises(AuthorizationException) as exc_info:
        await user_role_assignment_service.replace_roles(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            role_ids=[delegated_role.role_id],
        )

    assert exc_info.value.error_code == "USER_ROLE_AUTHORITY_EXCEEDED"
