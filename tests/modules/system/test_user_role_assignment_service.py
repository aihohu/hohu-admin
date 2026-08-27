"""Phase 2-B shared user role-assignment policy tests."""

from unittest.mock import AsyncMock

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.constants import (
    DATA_SCOPE_ALL,
    DATA_SCOPE_CUSTOM,
    DATA_SCOPE_DEPT_AND_SUB,
    DATA_SCOPE_SELF,
    IS_PRIMARY_NO,
    IS_PRIMARY_YES,
    STATUS_ENABLED,
    SUPER_ADMIN_ROLE_CODE,
    USER_ROLE_CODE,
)
from app.core.exceptions import AuthorizationException, BusinessRuleException
from app.core.id_generator import next_id
from app.db.base import user_depts
from app.modules.ai.models.agent import AiAgent
from app.modules.ai.models.role_ai_agent import RoleAiAgent
from app.modules.system.constants import USER_ROLE_AUTH_PERMISSION
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service.authorization_lock import authorization_lock_service
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


async def _seed_approved_role_replacement(
    db: AsyncSession,
    *,
    extra_actor_menus: list[Menu] | None = None,
) -> tuple[User, User, Role, Role, Role]:
    """Seed one valid role replacement that can be previewed and executed."""
    delegated_permission = _menu(f"qa:role-approved:{next_id()}:read")
    actor_role = _role(
        f"R_ROLE_APPROVED_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[
            _menu(USER_EDIT_PERMISSION),
            _menu(USER_ROLE_AUTH_PERMISSION),
            delegated_permission,
            *(extra_actor_menus or []),
        ],
    )
    old_role = _role(
        f"R_ROLE_APPROVED_OLD_{next_id()}",
        menus=[delegated_permission],
    )
    new_role = _role(
        f"R_ROLE_APPROVED_NEW_{next_id()}",
        menus=[delegated_permission],
    )
    actor = _user(f"phase2-role-approved-actor-{next_id()}", [actor_role])
    target = _user(f"phase2-role-approved-target-{next_id()}", [old_role])
    db.add_all([actor_role, old_role, new_role, actor, target])
    await db.flush()
    return actor, target, actor_role, old_role, new_role


async def test_bulk_authority_materialization_matches_single_policy(
    db_session: AsyncSession,
) -> None:
    root = Dept(
        dept_id=next_id(),
        dept_name=f"phase2-root-{next_id()}",
        ancestors="0",
        order_num=0,
        status=STATUS_ENABLED,
    )
    child = Dept(
        dept_id=next_id(),
        dept_name=f"phase2-child-{next_id()}",
        ancestors=f"0,{root.dept_id}",
        order_num=0,
        status=STATUS_ENABLED,
    )
    custom = Dept(
        dept_id=next_id(),
        dept_name=f"phase2-custom-{next_id()}",
        ancestors="0",
        order_num=0,
        status=STATUS_ENABLED,
    )
    subtree_role = _role(
        f"R_SUBTREE_{next_id()}",
        data_scope=DATA_SCOPE_DEPT_AND_SUB,
        menus=[_menu(f"qa:subtree:{next_id()}")],
    )
    custom_role = _role(
        f"R_CUSTOM_{next_id()}",
        data_scope=DATA_SCOPE_CUSTOM,
        menus=[_menu(f"qa:custom:{next_id()}")],
    )
    custom_role.depts = [custom]
    agent = AiAgent(
        agent_id=next_id(),
        code=f"phase2-bulk-agent-{next_id()}",
        name="Phase 2 bulk agent",
        description="Phase 2 bulk agent",
        enabled=True,
    )
    subtree_user = _user(f"phase2-subtree-{next_id()}", [subtree_role])
    custom_user = _user(f"phase2-custom-{next_id()}", [custom_role])
    child_member = _user(f"phase2-child-member-{next_id()}", [])
    custom_member = _user(f"phase2-custom-member-{next_id()}", [])
    db_session.add_all(
        [
            root,
            child,
            custom,
            subtree_role,
            custom_role,
            agent,
            subtree_user,
            custom_user,
            child_member,
            custom_member,
        ]
    )
    await db_session.flush()
    db_session.add(
        RoleAiAgent(
            role_id=custom_role.role_id,
            agent_id=agent.agent_id,
            enabled=True,
        )
    )
    await db_session.execute(
        insert(user_depts),
        [
            {"user_id": subtree_user.user_id, "dept_id": root.dept_id},
            {"user_id": child_member.user_id, "dept_id": child.dept_id},
            {"user_id": custom_member.user_id, "dept_id": custom.dept_id},
        ],
    )
    await db_session.flush()
    subtree_user = await user_role_assignment_service._load_user(
        db_session,
        subtree_user.user_id,
    )
    custom_user = await user_role_assignment_service._load_user(
        db_session,
        custom_user.user_id,
    )
    candidates = [
        (subtree_user, list(subtree_user.roles), list(subtree_user.depts)),
        (custom_user, list(custom_user.roles), list(custom_user.depts)),
    ]

    bulk = await user_role_assignment_service.materialize_role_set_authorities(
        db_session,
        candidates=candidates,
    )
    singles = [
        await user_role_assignment_service.materialize_role_set_authority(
            db_session,
            user=user,
            roles=roles,
            depts=depts,
        )
        for user, roles, depts in candidates
    ]

    assert bulk == singles


async def test_import_lock_includes_descendant_department_dependencies(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Dept(
        dept_id=next_id(),
        dept_name=f"phase2-import-root-{next_id()}",
        ancestors="0",
        order_num=0,
        status=STATUS_ENABLED,
    )
    child = Dept(
        dept_id=next_id(),
        dept_name=f"phase2-import-child-{next_id()}",
        parent_id=root.dept_id,
        ancestors=f"0,{root.dept_id}",
        order_num=0,
        status=STATUS_ENABLED,
    )
    subtree_role = _role(
        f"R_IMPORT_SUBTREE_{next_id()}",
        data_scope=DATA_SCOPE_DEPT_AND_SUB,
    )
    actor = _user(f"phase2-import-lock-{next_id()}", [subtree_role])
    db_session.add_all([root, child, subtree_role, actor])
    await db_session.flush()
    lock_targets = AsyncMock()
    monkeypatch.setattr(authorization_lock_service, "lock_targets", lock_targets)

    await user_role_assignment_service.lock_import_targets(
        db_session,
        actor_user_id=actor.user_id,
        target_user_ids=set(),
        role_ids=set(),
        dept_ids={root.dept_id},
    )

    locked_dept_ids = lock_targets.await_args.kwargs["dept_ids"]
    assert root.dept_id in locked_dept_ids
    assert child.dept_id in locked_dept_ids


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
    default_role = await db_session.scalar(
        select(Role)
        .where(Role.role_code == USER_ROLE_CODE)
        .options(selectinload(Role.menus))
    )
    assert default_role is not None
    default_role.menus = [
        *default_role.menus,
        _menu(f"qa:default-role-dominance:{next_id()}:read"),
    ]
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
    assert await user_role_assignment_service.roles_are_assignable(
        db_session,
        actor_user_id=actor.user_id,
        role_ids=[assignable.role_id],
    )
    assert not await user_role_assignment_service.roles_are_assignable(
        db_session,
        actor_user_id=actor.user_id,
        role_ids=[outside.role_id],
    )


async def test_preview_roles_freezes_complete_authorization_snapshot(
    db_session: AsyncSession,
) -> None:
    delegated_permission = _menu(f"qa:role-preview:{next_id()}:read")
    actor_role = _role(
        f"R_ROLE_PREVIEW_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[
            _menu(USER_EDIT_PERMISSION),
            _menu(USER_ROLE_AUTH_PERMISSION),
            delegated_permission,
        ],
    )
    old_role = _role(
        f"R_ROLE_PREVIEW_OLD_{next_id()}",
        menus=[delegated_permission],
    )
    new_role = _role(
        f"R_ROLE_PREVIEW_NEW_{next_id()}",
        menus=[delegated_permission],
    )
    actor = _user(f"phase2-role-preview-actor-{next_id()}", [actor_role])
    target = _user(f"phase2-role-preview-target-{next_id()}", [old_role])
    db_session.add_all(
        [delegated_permission, actor_role, old_role, new_role, actor, target]
    )
    await db_session.flush()

    preview = await user_role_assignment_service.preview_roles(
        db_session,
        actor_user_id=actor.user_id,
        target_user_id=target.user_id,
        role_ids=[new_role.role_id],
    )

    assert preview.old_role_ids == (old_role.role_id,)
    assert preview.new_role_ids == (new_role.role_id,)
    assert preview.old_display == (
        f"{old_role.role_name} ({old_role.role_code} / {old_role.role_id})",
    )
    assert preview.new_display == (
        f"{new_role.role_name} ({new_role.role_code} / {new_role.role_id})",
    )
    assert preview.snapshot["target"]["roleIds"] == [str(old_role.role_id)]
    assert preview.snapshot["oldRoleIds"] == [str(old_role.role_id)]
    assert preview.snapshot["newRoleIds"] == [str(new_role.role_id)]
    assert preview.snapshot["actor"]["authorityVersion"]
    assert preview.snapshot["before"]["authorizationHash"]
    assert preview.snapshot["after"]["scopeHash"]
    assert {fact["roleId"] for fact in preview.snapshot["roleFacts"]} == {
        str(old_role.role_id),
        str(new_role.role_id),
    }


async def test_replace_roles_rejects_approved_target_status_drift(
    db_session: AsyncSession,
) -> None:
    delegated_permission = _menu(f"qa:role-approved:{next_id()}:read")
    actor_role = _role(
        f"R_ROLE_APPROVED_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[
            _menu(USER_EDIT_PERMISSION),
            _menu(USER_ROLE_AUTH_PERMISSION),
            delegated_permission,
        ],
    )
    old_role = _role(
        f"R_ROLE_APPROVED_OLD_{next_id()}",
        menus=[delegated_permission],
    )
    new_role = _role(
        f"R_ROLE_APPROVED_NEW_{next_id()}",
        menus=[delegated_permission],
    )
    actor = _user(f"phase2-role-approved-actor-{next_id()}", [actor_role])
    target = _user(f"phase2-role-approved-target-{next_id()}", [old_role])
    db_session.add_all(
        [delegated_permission, actor_role, old_role, new_role, actor, target]
    )
    await db_session.flush()

    preview = await user_role_assignment_service.preview_roles(
        db_session,
        actor_user_id=actor.user_id,
        target_user_id=target.user_id,
        role_ids=[new_role.role_id],
    )
    target.status = "2"
    await db_session.flush()

    with pytest.raises(BusinessRuleException) as exc_info:
        await user_role_assignment_service.replace_roles(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            role_ids=[new_role.role_id],
            expected_snapshot=preview.snapshot,
        )

    assert exc_info.value.error_code == "AI_PREPARED_ACTION_SNAPSHOT_STALE"
    reloaded = await _reload_user(db_session, target.user_id)
    assert [role.role_id for role in reloaded.roles] == [old_role.role_id]


async def test_replace_roles_rejects_approved_candidate_menu_drift(
    db_session: AsyncSession,
) -> None:
    added_permission = _menu(f"qa:role-approved-menu:{next_id()}:read")
    (
        actor,
        target,
        _actor_role,
        old_role,
        new_role,
    ) = await _seed_approved_role_replacement(
        db_session,
        extra_actor_menus=[added_permission],
    )
    preview = await user_role_assignment_service.preview_roles(
        db_session,
        actor_user_id=actor.user_id,
        target_user_id=target.user_id,
        role_ids=[new_role.role_id],
    )
    new_role.menus.append(added_permission)
    await db_session.flush()

    with pytest.raises(BusinessRuleException) as exc_info:
        await user_role_assignment_service.replace_roles(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            role_ids=[new_role.role_id],
            expected_snapshot=preview.snapshot,
        )

    assert exc_info.value.error_code == "AI_PREPARED_ACTION_SNAPSHOT_STALE"
    reloaded = await _reload_user(db_session, target.user_id)
    assert [role.role_id for role in reloaded.roles] == [old_role.role_id]


async def test_replace_roles_rejects_approved_actor_authority_drift(
    db_session: AsyncSession,
) -> None:
    (
        actor,
        target,
        actor_role,
        old_role,
        new_role,
    ) = await _seed_approved_role_replacement(db_session)
    preview = await user_role_assignment_service.preview_roles(
        db_session,
        actor_user_id=actor.user_id,
        target_user_id=target.user_id,
        role_ids=[new_role.role_id],
    )
    actor_role.menus.append(_menu(f"qa:role-actor-drift:{next_id()}:read"))
    await db_session.flush()

    with pytest.raises(BusinessRuleException) as exc_info:
        await user_role_assignment_service.replace_roles(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            role_ids=[new_role.role_id],
            expected_snapshot=preview.snapshot,
        )

    assert exc_info.value.error_code == "AI_PREPARED_ACTION_SNAPSHOT_STALE"
    reloaded = await _reload_user(db_session, target.user_id)
    assert [role.role_id for role in reloaded.roles] == [old_role.role_id]


async def test_replace_roles_rejects_approved_primary_department_drift(
    db_session: AsyncSession,
) -> None:
    (
        actor,
        target,
        _actor_role,
        old_role,
        new_role,
    ) = await _seed_approved_role_replacement(db_session)
    dept = Dept(
        dept_id=next_id(),
        dept_name=f"phase2-role-approved-dept-{next_id()}",
        ancestors="0",
        order_num=0,
        status=STATUS_ENABLED,
    )
    db_session.add(dept)
    await db_session.flush()
    await db_session.execute(
        insert(user_depts).values(
            user_id=target.user_id,
            dept_id=dept.dept_id,
            is_primary=IS_PRIMARY_YES,
        )
    )
    preview = await user_role_assignment_service.preview_roles(
        db_session,
        actor_user_id=actor.user_id,
        target_user_id=target.user_id,
        role_ids=[new_role.role_id],
    )
    await db_session.execute(
        update(user_depts)
        .where(
            user_depts.c.user_id == target.user_id,
            user_depts.c.dept_id == dept.dept_id,
        )
        .values(is_primary=IS_PRIMARY_NO)
    )

    with pytest.raises(BusinessRuleException) as exc_info:
        await user_role_assignment_service.replace_roles(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            role_ids=[new_role.role_id],
            expected_snapshot=preview.snapshot,
        )

    assert exc_info.value.error_code == "AI_PREPARED_ACTION_SNAPSHOT_STALE"
    reloaded = await _reload_user(db_session, target.user_id)
    assert [role.role_id for role in reloaded.roles] == [old_role.role_id]


async def test_replace_roles_rejects_approved_role_agent_drift(
    db_session: AsyncSession,
) -> None:
    (
        actor,
        target,
        actor_role,
        old_role,
        new_role,
    ) = await _seed_approved_role_replacement(db_session)
    agent = AiAgent(
        agent_id=next_id(),
        code=f"phase2-approved-agent-{next_id()}",
        name="Phase 2 approved agent",
        description="Phase 2 approved agent",
        enabled=True,
    )
    db_session.add(agent)
    await db_session.flush()
    db_session.add(
        RoleAiAgent(
            role_id=actor_role.role_id,
            agent_id=agent.agent_id,
            enabled=True,
        )
    )
    await db_session.flush()
    preview = await user_role_assignment_service.preview_roles(
        db_session,
        actor_user_id=actor.user_id,
        target_user_id=target.user_id,
        role_ids=[new_role.role_id],
    )
    db_session.add(
        RoleAiAgent(
            role_id=new_role.role_id,
            agent_id=agent.agent_id,
            enabled=True,
        )
    )
    await db_session.flush()

    with pytest.raises(BusinessRuleException) as exc_info:
        await user_role_assignment_service.replace_roles(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            role_ids=[new_role.role_id],
            expected_snapshot=preview.snapshot,
        )

    assert exc_info.value.error_code == "AI_PREPARED_ACTION_SNAPSHOT_STALE"
    reloaded = await _reload_user(db_session, target.user_id)
    assert [role.role_id for role in reloaded.roles] == [old_role.role_id]


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
