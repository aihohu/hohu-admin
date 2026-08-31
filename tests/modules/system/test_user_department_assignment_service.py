"""Phase 2-B2 complete user-department replacement policy tests."""

from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import (
    ADMIN_USERNAME,
    DATA_SCOPE_ALL,
    DATA_SCOPE_CUSTOM,
    DATA_SCOPE_DEPT_AND_SUB,
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
from app.modules.system.models.config import Config
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service.authorization_lock import authorization_lock_service
from app.modules.system.service.grant_authority import grant_authority_service
from app.modules.system.service.user_department_assignment_service import (
    user_department_assignment_service,
)

USER_EDIT_PERMISSION = "system:user:edit"
USER_ADD_PERMISSION = "system:user:add"
USER_IMPORT_PERMISSION = "system:user:import"
DEPT_LIST_PERMISSION = "system:dept:list"


def _menu(permission: str) -> Menu:
    marker = next_id()
    return Menu(
        menu_id=marker,
        menu_name=f"phase2-dept-menu-{marker}",
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
        role_name=f"phase2-dept-role-{marker}",
        role_code=code,
        data_scope=data_scope,
        status=STATUS_ENABLED,
    )
    role.menus = menus or []
    return role


def _dept(name: str, *, parent: Dept | None = None) -> Dept:
    dept_id = next_id()
    ancestors = "0" if parent is None else f"{parent.ancestors},{parent.dept_id}"
    return Dept(
        dept_id=dept_id,
        dept_name=f"{name}-{dept_id}",
        parent_id=parent.dept_id if parent is not None else None,
        ancestors=ancestors,
        order_num=0,
        status=STATUS_ENABLED,
    )


def _user(name: str, roles: list[Role]) -> User:
    return User(
        user_id=next_id(),
        user_name=name,
        nickname=name,
        hashed_password="x",
        status=STATUS_ENABLED,
        roles=roles,
    )


async def _bind_dept(
    db: AsyncSession,
    user: User,
    dept: Dept,
    *,
    primary: bool,
) -> None:
    await db.execute(
        insert(user_depts).values(
            user_id=user.user_id,
            dept_id=dept.dept_id,
            is_primary=IS_PRIMARY_YES if primary else IS_PRIMARY_NO,
        )
    )


async def _assignments(
    db: AsyncSession,
    user_id: int,
) -> list[tuple[int, str]]:
    rows = (
        await db.execute(
            select(user_depts.c.dept_id, user_depts.c.is_primary)
            .where(user_depts.c.user_id == user_id)
            .order_by(user_depts.c.dept_id)
        )
    ).all()
    return [(int(dept_id), str(is_primary)) for dept_id, is_primary in rows]


async def test_replace_departments_applies_one_complete_authorized_set(
    db_session: AsyncSession,
) -> None:
    actor_role = _role(
        f"R_DEPT_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[_menu(USER_EDIT_PERMISSION), _menu(DEPT_LIST_PERMISSION)],
    )
    target_role = _role(f"R_DEPT_TARGET_{next_id()}")
    old_dept = _dept("phase2-old")
    new_dept = _dept("phase2-new")
    actor = _user(f"phase2-dept-actor-{next_id()}", [actor_role])
    target = _user(f"phase2-dept-target-{next_id()}", [target_role])
    db_session.add_all([actor_role, target_role, old_dept, new_dept, actor, target])
    await db_session.flush()
    await _bind_dept(db_session, target, old_dept, primary=True)

    with patch(
        "app.modules.system.service.user_department_assignment_service."
        "config_service.get_bool_for_update",
        AsyncMock(return_value=False),
    ):
        result = await user_department_assignment_service.replace_departments(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            dept_assignments=[(new_dept.dept_id, False)],
        )

    assert result.old_assignments == ((old_dept.dept_id, True),)
    assert result.new_assignments == ((new_dept.dept_id, False),)
    assert await _assignments(db_session, target.user_id) == [
        (new_dept.dept_id, IS_PRIMARY_NO)
    ]


async def test_preview_departments_freezes_complete_authorization_snapshot(
    db_session: AsyncSession,
) -> None:
    actor_role = _role(
        f"R_DEPT_PREVIEW_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[_menu(USER_EDIT_PERMISSION), _menu(DEPT_LIST_PERMISSION)],
    )
    target_role = _role(f"R_DEPT_PREVIEW_TARGET_{next_id()}")
    old_dept = _dept("phase2-preview-old")
    new_dept = _dept("phase2-preview-new")
    actor = _user(f"phase2-preview-actor-{next_id()}", [actor_role])
    target = _user(f"phase2-preview-target-{next_id()}", [target_role])
    db_session.add_all([actor_role, target_role, old_dept, new_dept, actor, target])
    await db_session.flush()
    await _bind_dept(db_session, target, old_dept, primary=True)

    with patch(
        "app.modules.system.service.user_department_assignment_service."
        "config_service.get_bool_for_update",
        AsyncMock(return_value=False),
    ):
        preview = await user_department_assignment_service.preview_departments(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            dept_assignments=[(new_dept.dept_id, False)],
        )

    assert preview.old_assignments == ((old_dept.dept_id, True),)
    assert preview.new_assignments == ((new_dept.dept_id, False),)
    assert preview.old_display == (f"★ {old_dept.dept_name}",)
    assert preview.new_display == (new_dept.dept_name,)
    assert preview.snapshot["target"] == {
        "userId": str(target.user_id),
        "userName": target.user_name,
        "status": STATUS_ENABLED,
        "roleIds": [str(target_role.role_id)],
    }
    assert preview.snapshot["oldAssignments"] == [
        {"deptId": str(old_dept.dept_id), "isPrimary": True}
    ]
    assert preview.snapshot["newAssignments"] == [
        {"deptId": str(new_dept.dept_id), "isPrimary": False}
    ]
    assert preview.snapshot["actor"]["authorityVersion"]
    assert preview.snapshot["before"]["authorizationHash"]
    assert preview.snapshot["after"]["scopeHash"]
    assert {fact["deptId"] for fact in preview.snapshot["departmentFacts"]} == {
        str(old_dept.dept_id),
        str(new_dept.dept_id),
    }


async def test_replace_departments_rejects_approved_target_status_drift(
    db_session: AsyncSession,
) -> None:
    actor_role = _role(
        f"R_DEPT_APPROVAL_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[_menu(USER_EDIT_PERMISSION), _menu(DEPT_LIST_PERMISSION)],
    )
    target_role = _role(f"R_DEPT_APPROVAL_TARGET_{next_id()}")
    old_dept = _dept("phase2-approval-old")
    new_dept = _dept("phase2-approval-new")
    actor = _user(f"phase2-approval-actor-{next_id()}", [actor_role])
    target = _user(f"phase2-approval-target-{next_id()}", [target_role])
    db_session.add_all([actor_role, target_role, old_dept, new_dept, actor, target])
    await db_session.flush()
    await _bind_dept(db_session, target, old_dept, primary=True)

    with patch(
        "app.modules.system.service.user_department_assignment_service."
        "config_service.get_bool_for_update",
        AsyncMock(return_value=False),
    ):
        preview = await user_department_assignment_service.preview_departments(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            dept_assignments=[(new_dept.dept_id, False)],
        )
        target.status = STATUS_DISABLED
        await db_session.flush()

        with pytest.raises(BusinessRuleException) as exc_info:
            await user_department_assignment_service.replace_departments(
                db_session,
                actor_user_id=actor.user_id,
                target_user_id=target.user_id,
                dept_assignments=[(new_dept.dept_id, False)],
                expected_snapshot=preview.snapshot,
            )

    assert exc_info.value.error_code == "AI_PREPARED_ACTION_SNAPSHOT_STALE"
    assert await _assignments(db_session, target.user_id) == [
        (old_dept.dept_id, IS_PRIMARY_YES)
    ]


async def test_assign_created_departments_uses_add_and_department_permissions(
    db_session: AsyncSession,
) -> None:
    dept = _dept("phase2-created")
    actor_role = _role(
        f"R_DEPT_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[_menu(USER_ADD_PERMISSION), _menu(DEPT_LIST_PERMISSION)],
    )
    target_role = _role(f"R_DEPT_TARGET_{next_id()}")
    actor = _user(f"phase2-dept-actor-{next_id()}", [actor_role])
    target = _user(f"phase2-dept-created-{next_id()}", [target_role])
    db_session.add_all([dept, actor_role, target_role, actor, target])
    await db_session.flush()

    with patch(
        "app.modules.system.service.user_department_assignment_service."
        "config_service.get_bool_for_update",
        AsyncMock(return_value=False),
    ):
        result = (
            await user_department_assignment_service.assign_created_user_departments(
                db_session,
                actor_user_id=actor.user_id,
                target_user_id=target.user_id,
                dept_assignments=[(dept.dept_id, True)],
            )
        )

    assert result.old_assignments == ()
    assert result.new_assignments == ((dept.dept_id, True),)
    assert await _assignments(db_session, target.user_id) == [
        (dept.dept_id, IS_PRIMARY_YES)
    ]


async def test_created_departments_require_department_list_permission(
    db_session: AsyncSession,
) -> None:
    dept = _dept("phase2-created")
    actor_role = _role(
        f"R_DEPT_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[_menu(USER_ADD_PERMISSION)],
    )
    target_role = _role(f"R_DEPT_TARGET_{next_id()}")
    actor = _user(f"phase2-dept-actor-{next_id()}", [actor_role])
    target = _user(f"phase2-dept-created-{next_id()}", [target_role])
    db_session.add_all([dept, actor_role, target_role, actor, target])
    await db_session.flush()

    with pytest.raises(AuthorizationException) as exc_info:
        await user_department_assignment_service.assign_created_user_departments(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            dept_assignments=[(dept.dept_id, True)],
        )

    assert exc_info.value.error_code == "MISSING_PERMISSION"
    assert await _assignments(db_session, target.user_id) == []


async def test_import_department_validation_cannot_delete_a_hidden_old_assignment(
    db_session: AsyncSession,
) -> None:
    visible_dept = _dept("phase2-visible")
    hidden_dept = _dept("phase2-hidden")
    actor_role = _role(
        f"R_DEPT_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_CUSTOM,
        menus=[_menu(USER_IMPORT_PERMISSION), _menu(DEPT_LIST_PERMISSION)],
    )
    actor_role.depts = [visible_dept]
    target_role = _role(f"R_DEPT_TARGET_{next_id()}")
    actor = _user(f"phase2-dept-actor-{next_id()}", [actor_role])
    target = _user(f"phase2-dept-target-{next_id()}", [target_role])
    db_session.add_all(
        [visible_dept, hidden_dept, actor_role, target_role, actor, target]
    )
    await db_session.flush()
    await _bind_dept(db_session, actor, visible_dept, primary=True)
    await _bind_dept(db_session, target, visible_dept, primary=True)
    await _bind_dept(db_session, target, hidden_dept, primary=False)
    authority = await grant_authority_service.build(db_session, actor.user_id)

    with pytest.raises(AuthorizationException) as exc_info:
        await user_department_assignment_service.validate_import_department_assignment(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            target_user_name=target.user_name,
            target_status=target.status,
            role_ids=[target_role.role_id],
            dept_assignments=[(visible_dept.dept_id, True)],
            authority=authority,
        )

    assert exc_info.value.error_code == "AI_DATA_SCOPE_VIOLATION"


async def test_replace_departments_revalidates_both_entry_permissions(
    db_session: AsyncSession,
) -> None:
    actor_role = _role(
        f"R_DEPT_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[_menu(USER_EDIT_PERMISSION)],
    )
    target_role = _role(f"R_DEPT_TARGET_{next_id()}")
    actor = _user(f"phase2-dept-actor-{next_id()}", [actor_role])
    target = _user(f"phase2-dept-target-{next_id()}", [target_role])
    db_session.add_all([actor_role, target_role, actor, target])
    await db_session.flush()

    with pytest.raises(AuthorizationException) as exc_info:
        await user_department_assignment_service.replace_departments(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            dept_assignments=[],
        )

    assert exc_info.value.error_code == "MISSING_PERMISSION"


async def test_replace_departments_rejects_any_direct_scope_violation(
    db_session: AsyncSession,
) -> None:
    old_dept = _dept("phase2-visible")
    outside_dept = _dept("phase2-outside")
    actor_role = _role(
        f"R_DEPT_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_CUSTOM,
        menus=[_menu(USER_EDIT_PERMISSION), _menu(DEPT_LIST_PERMISSION)],
    )
    actor_role.depts = [old_dept]
    target_role = _role(f"R_DEPT_TARGET_{next_id()}")
    actor = _user(f"phase2-dept-actor-{next_id()}", [actor_role])
    target = _user(f"phase2-dept-target-{next_id()}", [target_role])
    db_session.add_all([old_dept, outside_dept, actor_role, target_role, actor, target])
    await db_session.flush()
    await _bind_dept(db_session, actor, old_dept, primary=True)
    await _bind_dept(db_session, target, old_dept, primary=True)

    with (
        patch(
            "app.modules.system.service.user_department_assignment_service."
            "config_service.get_bool_for_update",
            AsyncMock(return_value=False),
        ),
        pytest.raises(AuthorizationException) as exc_info,
    ):
        await user_department_assignment_service.replace_departments(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            dept_assignments=[(outside_dept.dept_id, True)],
        )

    assert exc_info.value.error_code == "AI_DATA_SCOPE_VIOLATION"
    assert await _assignments(db_session, target.user_id) == [
        (old_dept.dept_id, IS_PRIMARY_YES)
    ]


async def test_replace_departments_cannot_delete_a_hidden_old_assignment(
    db_session: AsyncSession,
) -> None:
    visible_dept = _dept("phase2-visible")
    hidden_dept = _dept("phase2-hidden")
    actor_role = _role(
        f"R_DEPT_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_CUSTOM,
        menus=[_menu(USER_EDIT_PERMISSION), _menu(DEPT_LIST_PERMISSION)],
    )
    actor_role.depts = [visible_dept]
    target_role = _role(f"R_DEPT_TARGET_{next_id()}")
    actor = _user(f"phase2-dept-actor-{next_id()}", [actor_role])
    target = _user(f"phase2-dept-target-{next_id()}", [target_role])
    db_session.add_all(
        [visible_dept, hidden_dept, actor_role, target_role, actor, target]
    )
    await db_session.flush()
    await _bind_dept(db_session, actor, visible_dept, primary=True)
    await _bind_dept(db_session, target, visible_dept, primary=True)
    await _bind_dept(db_session, target, hidden_dept, primary=False)

    with pytest.raises(AuthorizationException) as exc_info:
        await user_department_assignment_service.replace_departments(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            dept_assignments=[(visible_dept.dept_id, True)],
        )

    assert exc_info.value.error_code == "AI_DATA_SCOPE_VIOLATION"
    assert await _assignments(db_session, target.user_id) == [
        (visible_dept.dept_id, IS_PRIMARY_YES),
        (hidden_dept.dept_id, IS_PRIMARY_NO),
    ]


async def test_replace_departments_rejects_materialized_subtree_impact_atomically(
    db_session: AsyncSession,
) -> None:
    old_leaf = _dept("phase2-old-leaf")
    new_parent = _dept("phase2-new-parent")
    hidden_child = _dept("phase2-hidden-child", parent=new_parent)
    actor_role = _role(
        f"R_DEPT_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_CUSTOM,
        menus=[_menu(USER_EDIT_PERMISSION), _menu(DEPT_LIST_PERMISSION)],
    )
    actor_role.depts = [old_leaf, new_parent]
    target_role = _role(
        f"R_DEPT_TARGET_{next_id()}",
        data_scope=DATA_SCOPE_DEPT_AND_SUB,
    )
    actor = _user(f"phase2-dept-actor-{next_id()}", [actor_role])
    target = _user(f"phase2-dept-target-{next_id()}", [target_role])
    db_session.add_all(
        [
            old_leaf,
            new_parent,
            hidden_child,
            actor_role,
            target_role,
            actor,
            target,
        ]
    )
    await db_session.flush()
    await _bind_dept(db_session, actor, old_leaf, primary=True)
    await _bind_dept(db_session, target, old_leaf, primary=True)

    with (
        patch(
            "app.modules.system.service.user_department_assignment_service."
            "config_service.get_bool_for_update",
            AsyncMock(return_value=False),
        ),
        pytest.raises(AuthorizationException) as exc_info,
    ):
        await user_department_assignment_service.replace_departments(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            dept_assignments=[(new_parent.dept_id, True)],
        )

    assert exc_info.value.error_code == ("AI_USER_DEPT_AUTHZ_IMPACT_OUT_OF_SCOPE")
    assert await _assignments(db_session, target.user_id) == [
        (old_leaf.dept_id, IS_PRIMARY_YES)
    ]


async def test_replace_departments_also_rejects_existing_out_of_bound_impact(
    db_session: AsyncSession,
) -> None:
    old_parent = _dept("phase2-old-parent")
    hidden_child = _dept("phase2-hidden-child", parent=old_parent)
    safe_leaf = _dept("phase2-safe-leaf")
    actor_role = _role(
        f"R_DEPT_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_CUSTOM,
        menus=[_menu(USER_EDIT_PERMISSION), _menu(DEPT_LIST_PERMISSION)],
    )
    actor_role.depts = [old_parent, safe_leaf]
    target_role = _role(
        f"R_DEPT_TARGET_{next_id()}",
        data_scope=DATA_SCOPE_DEPT_AND_SUB,
    )
    actor = _user(f"phase2-dept-actor-{next_id()}", [actor_role])
    target = _user(f"phase2-dept-target-{next_id()}", [target_role])
    db_session.add_all(
        [
            old_parent,
            hidden_child,
            safe_leaf,
            actor_role,
            target_role,
            actor,
            target,
        ]
    )
    await db_session.flush()
    await _bind_dept(db_session, actor, old_parent, primary=True)
    await _bind_dept(db_session, target, old_parent, primary=True)

    with (
        patch(
            "app.modules.system.service.user_department_assignment_service."
            "config_service.get_bool_for_update",
            AsyncMock(return_value=False),
        ),
        pytest.raises(AuthorizationException) as exc_info,
    ):
        await user_department_assignment_service.replace_departments(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            dept_assignments=[(safe_leaf.dept_id, True)],
        )

    assert exc_info.value.error_code == ("AI_USER_DEPT_AUTHZ_IMPACT_OUT_OF_SCOPE")
    assert await _assignments(db_session, target.user_id) == [
        (old_parent.dept_id, IS_PRIMARY_YES)
    ]


async def test_replace_departments_rejects_disabled_assignment_targets(
    db_session: AsyncSession,
) -> None:
    actor_role = _role(
        f"R_DEPT_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[_menu(USER_EDIT_PERMISSION), _menu(DEPT_LIST_PERMISSION)],
    )
    target_role = _role(f"R_DEPT_TARGET_{next_id()}")
    disabled_dept = _dept("phase2-disabled")
    disabled_dept.status = STATUS_DISABLED
    actor = _user(f"phase2-dept-actor-{next_id()}", [actor_role])
    target = _user(f"phase2-dept-target-{next_id()}", [target_role])
    db_session.add_all([actor_role, target_role, disabled_dept, actor, target])
    await db_session.flush()

    with pytest.raises(BusinessRuleException) as exc_info:
        await user_department_assignment_service.replace_departments(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            dept_assignments=[(disabled_dept.dept_id, True)],
        )

    assert exc_info.value.error_code == "USER_DEPT_NOT_AVAILABLE"


async def test_replace_departments_enforces_live_primary_department_policy(
    db_session: AsyncSession,
) -> None:
    actor_role = _role(
        f"R_DEPT_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[_menu(USER_EDIT_PERMISSION), _menu(DEPT_LIST_PERMISSION)],
    )
    target_role = _role(f"R_DEPT_TARGET_{next_id()}")
    first = _dept("phase2-first")
    second = _dept("phase2-second")
    actor = _user(f"phase2-dept-actor-{next_id()}", [actor_role])
    target = _user(f"phase2-dept-target-{next_id()}", [target_role])
    db_session.add_all([actor_role, target_role, first, second, actor, target])
    await db_session.flush()

    with (
        patch(
            "app.modules.system.service.user_department_assignment_service."
            "config_service.get_bool_for_update",
            AsyncMock(return_value=True),
        ),
        pytest.raises(BusinessRuleException) as required_exc,
    ):
        await user_department_assignment_service.replace_departments(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            dept_assignments=[],
        )
    assert required_exc.value.error_code == "USER_PRIMARY_DEPT_REQUIRED"

    with (
        patch(
            "app.modules.system.service.user_department_assignment_service."
            "config_service.get_bool_for_update",
            AsyncMock(return_value=False),
        ),
        pytest.raises(BusinessRuleException) as multiple_exc,
    ):
        await user_department_assignment_service.replace_departments(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            dept_assignments=[
                (first.dept_id, True),
                (second.dept_id, True),
            ],
        )
    assert multiple_exc.value.error_code == "USER_PRIMARY_DEPT_MULTIPLE"


async def test_replace_departments_bypasses_stale_primary_policy_cache(
    db_session: AsyncSession,
) -> None:
    actor_role = _role(
        f"R_DEPT_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[_menu(USER_EDIT_PERMISSION), _menu(DEPT_LIST_PERMISSION)],
    )
    target_role = _role(f"R_DEPT_TARGET_{next_id()}")
    actor = _user(f"phase2-dept-actor-{next_id()}", [actor_role])
    target = _user(f"phase2-dept-target-{next_id()}", [target_role])
    policy = await db_session.scalar(
        select(Config).where(Config.config_key == "user_require_primary_dept")
    )
    if policy is None:
        policy = Config(
            config_name="Primary department policy test fixture",
            config_key="user_require_primary_dept",
            config_value="false",
            config_type="text",
            config_group="feature",
            status=STATUS_ENABLED,
            is_public=False,
        )
        db_session.add(policy)
    policy.config_value = "true"
    policy.status = STATUS_ENABLED
    db_session.add_all([actor_role, target_role, actor, target])
    await db_session.flush()

    with (
        patch(
            "app.modules.system.service.user_department_assignment_service."
            "config_service.get_bool",
            AsyncMock(return_value=False),
        ) as cached_get_bool,
        pytest.raises(BusinessRuleException) as exc_info,
    ):
        await user_department_assignment_service.replace_departments(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            dept_assignments=[],
        )

    assert exc_info.value.error_code == "USER_PRIMARY_DEPT_REQUIRED"
    cached_get_bool.assert_not_awaited()


async def test_replace_departments_protects_admin_and_super_role_targets(
    db_session: AsyncSession,
) -> None:
    actor_role = _role(
        f"R_DEPT_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[_menu(USER_EDIT_PERMISSION), _menu(DEPT_LIST_PERMISSION)],
    )
    super_role = await db_session.scalar(
        select(Role).where(Role.role_code == SUPER_ADMIN_ROLE_CODE)
    )
    admin_target = await db_session.scalar(
        select(User).where(User.user_name == ADMIN_USERNAME)
    )
    assert super_role is not None
    assert admin_target is not None
    actor = _user(f"phase2-dept-actor-{next_id()}", [actor_role])
    super_target = _user(f"phase2-super-target-{next_id()}", [super_role])
    db_session.add_all([actor_role, actor, super_target])
    await db_session.flush()

    for target in (admin_target, super_target):
        with pytest.raises(AuthorizationException) as exc_info:
            await user_department_assignment_service.replace_departments(
                db_session,
                actor_user_id=actor.user_id,
                target_user_id=target.user_id,
                dept_assignments=[],
            )
        assert exc_info.value.error_code == "AI_SUPER_ADMIN_REQUIRED"


async def test_replace_departments_rejects_a_changed_locked_snapshot(
    db_session: AsyncSession,
) -> None:
    actor_role = _role(
        f"R_DEPT_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[_menu(USER_EDIT_PERMISSION), _menu(DEPT_LIST_PERMISSION)],
    )
    target_role = _role(f"R_DEPT_TARGET_{next_id()}")
    old_dept = _dept("phase2-old")
    actor = _user(f"phase2-dept-actor-{next_id()}", [actor_role])
    target = _user(f"phase2-dept-target-{next_id()}", [target_role])
    db_session.add_all([actor_role, target_role, old_dept, actor, target])
    await db_session.flush()
    await _bind_dept(db_session, target, old_dept, primary=True)
    real_lock_targets = authorization_lock_service.lock_targets

    async def drift_then_lock(db: AsyncSession, **kwargs):  # noqa: ANN003
        await db.execute(
            update(user_depts)
            .where(
                user_depts.c.user_id == target.user_id,
                user_depts.c.dept_id == old_dept.dept_id,
            )
            .values(is_primary=IS_PRIMARY_NO)
        )
        return await real_lock_targets(db, **kwargs)

    with (
        patch.object(
            authorization_lock_service,
            "lock_targets",
            side_effect=drift_then_lock,
        ),
        pytest.raises(BusinessRuleException) as exc_info,
    ):
        await user_department_assignment_service.replace_departments(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            dept_assignments=[(old_dept.dept_id, True)],
        )

    assert exc_info.value.error_code == "AUTHORIZATION_SNAPSHOT_STALE"


async def test_replace_departments_rejects_new_scope_dependencies_after_lock(
    db_session: AsyncSession,
) -> None:
    actor_role = _role(
        f"R_DEPT_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_ALL,
        menus=[_menu(USER_EDIT_PERMISSION), _menu(DEPT_LIST_PERMISSION)],
    )
    target_role = _role(
        f"R_DEPT_TARGET_{next_id()}",
        data_scope=DATA_SCOPE_DEPT_AND_SUB,
    )
    new_parent = _dept("phase2-new-parent")
    actor = _user(f"phase2-dept-actor-{next_id()}", [actor_role])
    target = _user(f"phase2-dept-target-{next_id()}", [target_role])
    db_session.add_all([actor_role, target_role, new_parent, actor, target])
    await db_session.flush()
    real_lock_targets = authorization_lock_service.lock_targets

    async def add_dependency_then_lock(db: AsyncSession, **kwargs):  # noqa: ANN003
        db.add(_dept("phase2-late-child", parent=new_parent))
        await db.flush()
        return await real_lock_targets(db, **kwargs)

    with (
        patch.object(
            authorization_lock_service,
            "lock_targets",
            side_effect=add_dependency_then_lock,
        ),
        pytest.raises(BusinessRuleException) as exc_info,
    ):
        await user_department_assignment_service.replace_departments(
            db_session,
            actor_user_id=actor.user_id,
            target_user_id=target.user_id,
            dept_assignments=[(new_parent.dept_id, True)],
        )

    assert exc_info.value.error_code == "AUTHORIZATION_SNAPSHOT_STALE"
