"""Phase 3 department scope, indirect authorization, and lock tests."""

import inspect
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from app.constants import (
    DATA_SCOPE_CUSTOM,
    DATA_SCOPE_DEPT,
    DATA_SCOPE_DEPT_AND_SUB,
    DATA_SCOPE_SELF,
    IS_PRIMARY_NO,
    IS_PRIMARY_YES,
    STATUS_DISABLED,
    STATUS_ENABLED,
    SUPER_ADMIN_ROLE_CODE,
)
from app.core.exceptions import BusinessException
from app.core.id_generator import next_id
from app.db.base import user_depts, user_roles
from app.modules.system.models.dept import Dept
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.schemas.dept import DeptCreate, DeptUpdate
from app.modules.system.service.authorization_lock import authorization_lock_service
from app.modules.system.service.dept_service import dept_service
from app.modules.system.service.tenant_association_writer import (
    replace_role_depts,
    replace_role_menus,
    replace_user_roles,
)
from tests.tenant_helpers import tenant_context


def _menu(permission: str) -> Menu:
    marker = next_id()
    return Menu(
        tenant_id=0,
        menu_id=marker,
        menu_name=f"phase3-dept-menu-{marker}",
        menu_type="F",
        permission=permission,
        status=STATUS_ENABLED,
    )


def _role(
    code: str,
    *,
    data_scope: str = DATA_SCOPE_SELF,
    permissions: tuple[str, ...] = (),
) -> Role:
    marker = next_id()
    role = Role(
        tenant_id=0,
        role_id=marker,
        role_name=f"phase3-dept-role-{marker}",
        role_code=code,
        data_scope=data_scope,
        status=STATUS_ENABLED,
    )
    role.menus = [_menu(permission) for permission in permissions]
    return role


def _department(
    name: str,
    *,
    parent: Dept | None = None,
    status: str = STATUS_ENABLED,
) -> Dept:
    dept_id = next_id()
    return Dept(
        tenant_id=0,
        dept_id=dept_id,
        parent_id=parent.dept_id if parent is not None else None,
        ancestors=("0" if parent is None else f"{parent.ancestors},{parent.dept_id}"),
        dept_name=f"{name}-{dept_id}",
        order_num=0,
        status=status,
    )


def _user(name: str, roles: list[Role]) -> User:
    marker = next_id()
    return User(
        tenant_id=0,
        user_id=marker,
        user_name=f"{name}-{marker}",
        nickname=name,
        hashed_password="x",
        status=STATUS_ENABLED,
        roles=roles,
    )


async def _bind_department(
    db: AsyncSession,
    user: User,
    department: Dept,
    *,
    primary: bool,
) -> None:
    await db.execute(
        insert(user_depts).values(
            tenant_id=0,
            user_id=user.user_id,
            dept_id=department.dept_id,
            is_primary=IS_PRIMARY_YES if primary else IS_PRIMARY_NO,
        )
    )


def _tenant(actor: User):
    return tenant_context(tenant_id=0, actor_user_id=actor.user_id)


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
    for user, roles_for_user in user_links:
        await replace_user_roles(db, user, roles_for_user, tenant=tenant)
    for role, menus in role_menu_links:
        await replace_role_menus(db, role, menus, tenant=tenant)
    for role, depts in role_dept_links:
        await replace_role_depts(db, role, depts, tenant=tenant)
    await db.flush()


def test_shared_department_service_exposes_preview_and_snapshot_execution() -> None:
    for action in ("create", "update", "move"):
        preview = getattr(dept_service, f"preview_{action}", None)
        execute = getattr(dept_service, action, None)

        assert preview is not None
        assert execute is not None
        assert "actor_user_id" in inspect.signature(preview).parameters
        assert "actor_user_id" in inspect.signature(execute).parameters
        assert "expected_snapshot" in inspect.signature(execute).parameters
        assert ".commit(" not in inspect.getsource(preview)
        assert ".commit(" not in inspect.getsource(execute)


async def test_create_allows_a_scoped_child_and_rejects_hidden_parent_atomically(
    db_session: AsyncSession,
) -> None:
    visible_parent = _department("phase3-create-visible")
    hidden_parent = _department("phase3-create-hidden")
    actor_role = _role(
        f"R_PHASE3_CREATE_{next_id()}",
        data_scope=DATA_SCOPE_DEPT_AND_SUB,
        permissions=("system:dept:add", "system:dept:list"),
    )
    actor = _user("phase3-create-actor", [actor_role])
    await _persist_graph(db_session, visible_parent, hidden_parent, actor_role, actor)
    await _bind_department(db_session, actor, visible_parent, primary=True)

    created = await dept_service.create(
        db_session,
        DeptCreate(
            parentId=visible_parent.dept_id,
            deptName=f"phase3-created-{next_id()}",
            orderNum=0,
            status=STATUS_ENABLED,
        ),
        actor_user_id=actor.user_id,
        expected_snapshot=None,
        tenant=_tenant(actor),
    )

    assert created.parent_id == visible_parent.dept_id
    hidden_name = f"phase3-hidden-child-{next_id()}"
    with pytest.raises(BusinessException) as exc_info:
        await dept_service.create(
            db_session,
            DeptCreate(
                parentId=hidden_parent.dept_id,
                deptName=hidden_name,
                orderNum=0,
                status=STATUS_ENABLED,
            ),
            actor_user_id=actor.user_id,
            expected_snapshot=None,
            tenant=_tenant(actor),
        )

    assert exc_info.value.error_code == "AI_DATA_SCOPE_VIOLATION"
    assert (
        await db_session.scalar(
            select(func.count(Dept.dept_id)).where(
                Dept.tenant_id == 0,
                Dept.dept_name == hidden_name,
            )
        )
        == 0
    )


async def test_non_super_admin_cannot_create_a_tenant_root(
    db_session: AsyncSession,
) -> None:
    actor_dept = _department("phase3-root-actor")
    actor_role = _role(
        f"R_PHASE3_ROOT_{next_id()}",
        data_scope=DATA_SCOPE_DEPT,
        permissions=("system:dept:add", "system:dept:list"),
    )
    actor = _user("phase3-root-actor", [actor_role])
    await _persist_graph(db_session, actor_dept, actor_role, actor)
    await _bind_department(db_session, actor, actor_dept, primary=True)

    with pytest.raises(BusinessException):
        await dept_service.create(
            db_session,
            DeptCreate(
                parentId=None,
                deptName=f"phase3-forbidden-root-{next_id()}",
                orderNum=0,
                status=STATUS_ENABLED,
            ),
            actor_user_id=actor.user_id,
            expected_snapshot=None,
            tenant=_tenant(actor),
        )


async def test_department_leader_resolves_only_inside_actor_user_scope(
    db_session: AsyncSession,
) -> None:
    actor_root = _department("phase3-leader-root")
    hidden_root = _department("phase3-leader-hidden")
    target = _department("phase3-leader-target", parent=actor_root)
    actor_role = _role(
        f"R_PHASE3_LEADER_{next_id()}",
        data_scope=DATA_SCOPE_DEPT_AND_SUB,
        permissions=("system:dept:edit", "system:dept:list"),
    )
    actor = _user("phase3-leader-actor", [actor_role])
    hidden = _user("phase3-leader-hidden-user", [])
    await _persist_graph(
        db_session, actor_root, hidden_root, target, actor_role, actor, hidden
    )
    await _bind_department(db_session, actor, actor_root, primary=True)
    await _bind_department(db_session, hidden, hidden_root, primary=True)

    with pytest.raises(BusinessException) as exc_info:
        await dept_service.preview_update(
            db_session,
            target.dept_id,
            DeptUpdate(leader=hidden.user_name),
            actor_user_id=actor.user_id,
            tenant=_tenant(actor),
        )

    assert exc_info.value.error_code == "AI_DEPT_LEADER_NOT_FOUND"

    preview = await dept_service.preview_update(
        db_session,
        target.dept_id,
        DeptUpdate(leader=actor.user_name),
        actor_user_id=actor.user_id,
        tenant=_tenant(actor),
    )
    assert preview.snapshot["facts"]["leader"]["userId"] == str(actor.user_id)
    assert actor.user_id in preview.snapshot["facts"]["userIds"]


async def test_update_and_move_reject_direct_out_of_scope_targets(
    db_session: AsyncSession,
) -> None:
    actor_root = _department("phase3-direct-root")
    source = _department("phase3-direct-source", parent=actor_root)
    hidden = _department("phase3-direct-hidden")
    actor_role = _role(
        f"R_PHASE3_DIRECT_{next_id()}",
        data_scope=DATA_SCOPE_DEPT_AND_SUB,
        permissions=(
            "system:dept:edit",
            "system:dept:move",
            "system:dept:list",
        ),
    )
    actor = _user("phase3-direct-actor", [actor_role])
    await _persist_graph(db_session, actor_root, source, hidden, actor_role, actor)
    await _bind_department(db_session, actor, actor_root, primary=True)

    original_name = hidden.dept_name
    with pytest.raises(BusinessException) as update_error:
        await dept_service.update(
            db_session,
            hidden.dept_id,
            DeptUpdate(deptName=f"phase3-forbidden-update-{next_id()}"),
            actor_user_id=actor.user_id,
            expected_snapshot=None,
            tenant=_tenant(actor),
        )
    assert update_error.value.error_code == "AI_DATA_SCOPE_VIOLATION"
    assert hidden.dept_name == original_name

    move = getattr(dept_service, "move", None)
    assert move is not None
    with pytest.raises(BusinessException) as move_error:
        await move(
            db_session,
            dept_id=source.dept_id,
            new_parent_id=hidden.dept_id,
            actor_user_id=actor.user_id,
            expected_snapshot=None,
            tenant=_tenant(actor),
        )
    assert move_error.value.error_code == "AI_DATA_SCOPE_VIOLATION"
    assert source.parent_id == actor_root.dept_id


async def test_non_super_admin_cannot_move_a_delegated_scope_root(
    db_session: AsyncSession,
) -> None:
    hidden_parent = _department("phase3-scope-root-hidden")
    scope_root = _department("phase3-scope-root-source", parent=hidden_parent)
    new_parent = _department("phase3-scope-root-destination")
    actor_role = _role(
        f"R_PHASE3_SCOPE_ROOT_{next_id()}",
        data_scope=DATA_SCOPE_CUSTOM,
        permissions=("system:dept:move", "system:dept:list"),
    )
    actor_role.depts = [scope_root, new_parent]
    actor = _user("phase3-scope-root-actor", [actor_role])
    await _persist_graph(
        db_session, hidden_parent, scope_root, new_parent, actor_role, actor
    )

    with pytest.raises(BusinessException) as exc_info:
        await dept_service.move(
            db_session,
            dept_id=scope_root.dept_id,
            new_parent_id=new_parent.dept_id,
            actor_user_id=actor.user_id,
            tenant=_tenant(actor),
        )

    assert exc_info.value.error_code == "AI_DEPT_SCOPE_ROOT_MOVE_FORBIDDEN"
    assert scope_root.parent_id == hidden_parent.dept_id


async def test_status_change_rejects_an_out_of_scope_affected_principal(
    db_session: AsyncSession,
) -> None:
    target = _department("phase3-status-target", status=STATUS_DISABLED)
    outside = _department("phase3-status-outside")
    actor_role = _role(
        f"R_PHASE3_STATUS_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_DEPT,
        permissions=("system:dept:edit", "system:dept:list"),
    )
    affected_role = _role(
        f"R_PHASE3_STATUS_AFFECTED_{next_id()}",
        data_scope=DATA_SCOPE_CUSTOM,
    )
    affected_role.depts = [target]
    actor = _user("phase3-status-actor", [actor_role])
    affected = _user("phase3-status-affected", [affected_role])
    await _persist_graph(
        db_session, target, outside, actor_role, affected_role, actor, affected
    )
    await _bind_department(db_session, actor, target, primary=True)
    await _bind_department(db_session, affected, outside, primary=True)

    with pytest.raises(BusinessException) as exc_info:
        await dept_service.update(
            db_session,
            target.dept_id,
            DeptUpdate(status=STATUS_ENABLED),
            actor_user_id=actor.user_id,
            expected_snapshot=None,
            tenant=_tenant(actor),
        )

    assert exc_info.value.error_code == "AI_DEPT_STATUS_AUTHZ_IMPACT_OUT_OF_SCOPE"
    assert target.status == STATUS_DISABLED


async def test_status_change_checks_affected_role_without_members(
    db_session: AsyncSession,
) -> None:
    target = _department("phase3-status-role-only", status=STATUS_DISABLED)
    actor_role = _role(
        f"R_PHASE3_STATUS_ROLE_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_DEPT,
        permissions=("system:dept:edit", "system:dept:list"),
    )
    hidden_permission = _menu("system:hidden:permission")
    affected_role = _role(
        f"R_PHASE3_STATUS_ROLE_ONLY_{next_id()}",
        data_scope=DATA_SCOPE_CUSTOM,
    )
    affected_role.depts = [target]
    affected_role.menus = [hidden_permission]
    actor = _user("phase3-status-role-actor", [actor_role])
    await _persist_graph(db_session, target, actor_role, affected_role, actor)
    await _bind_department(db_session, actor, target, primary=True)

    with pytest.raises(BusinessException) as exc_info:
        await dept_service.update(
            db_session,
            target.dept_id,
            DeptUpdate(status=STATUS_ENABLED),
            actor_user_id=actor.user_id,
            tenant=_tenant(actor),
        )

    assert exc_info.value.error_code == "AI_DEPT_STATUS_AUTHZ_IMPACT_OUT_OF_SCOPE"
    assert target.status == STATUS_DISABLED


async def test_move_rejects_materialized_scope_outside_actor_authority(
    db_session: AsyncSession,
) -> None:
    actor_root = _department("phase3-move-root")
    old_parent = _department("phase3-move-old", parent=actor_root)
    new_parent = _department("phase3-move-new", parent=actor_root)
    source = _department("phase3-move-source", parent=old_parent)
    outside = _department("phase3-move-outside")
    actor_role = _role(
        f"R_PHASE3_MOVE_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_DEPT_AND_SUB,
        permissions=("system:dept:move", "system:dept:list"),
    )
    affected_role = _role(
        f"R_PHASE3_MOVE_AFFECTED_{next_id()}",
        data_scope=DATA_SCOPE_DEPT_AND_SUB,
    )
    actor = _user("phase3-move-actor", [actor_role])
    affected = _user("phase3-move-affected", [affected_role])
    await _persist_graph(
        db_session,
        actor_root,
        old_parent,
        new_parent,
        source,
        outside,
        actor_role,
        affected_role,
        actor,
        affected,
    )
    await _bind_department(db_session, actor, actor_root, primary=True)
    await _bind_department(db_session, affected, old_parent, primary=True)
    await _bind_department(db_session, affected, outside, primary=False)
    move = getattr(dept_service, "move", None)

    assert move is not None
    with pytest.raises(BusinessException) as exc_info:
        await move(
            db_session,
            dept_id=source.dept_id,
            new_parent_id=new_parent.dept_id,
            actor_user_id=actor.user_id,
            expected_snapshot=None,
            tenant=_tenant(actor),
        )

    assert exc_info.value.error_code == "AI_DEPT_MOVE_AUTHZ_IMPACT_OUT_OF_SCOPE"
    assert source.parent_id == old_parent.dept_id


async def test_move_locks_complete_role_department_user_dependencies_in_order(
    db_session: AsyncSession,
) -> None:
    actor_root = _department("phase3-lock-root")
    old_parent = _department("phase3-lock-old", parent=actor_root)
    new_parent = _department("phase3-lock-new", parent=actor_root)
    source = _department("phase3-lock-source", parent=old_parent)
    child = _department("phase3-lock-child", parent=source)
    actor_role = _role(
        f"R_PHASE3_LOCK_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_DEPT_AND_SUB,
        permissions=("system:dept:move", "system:dept:list"),
    )
    affected_role = _role(
        f"R_PHASE3_LOCK_AFFECTED_{next_id()}",
        data_scope=DATA_SCOPE_DEPT_AND_SUB,
    )
    actor = _user("phase3-lock-actor", [actor_role])
    affected = _user("phase3-lock-affected", [affected_role])
    await _persist_graph(
        db_session,
        actor_root,
        old_parent,
        new_parent,
        source,
        child,
        actor_role,
        affected_role,
        actor,
        affected,
    )
    await _bind_department(db_session, actor, actor_root, primary=True)
    await _bind_department(db_session, affected, old_parent, primary=True)
    lock_spy = AsyncMock(wraps=authorization_lock_service.lock_targets)
    move = getattr(dept_service, "move", None)

    assert move is not None
    with patch.object(authorization_lock_service, "lock_targets", lock_spy):
        await move(
            db_session,
            dept_id=source.dept_id,
            new_parent_id=new_parent.dept_id,
            actor_user_id=actor.user_id,
            expected_snapshot=None,
            tenant=_tenant(actor),
        )

    assert lock_spy.await_count == 1
    lock_args = lock_spy.await_args.kwargs
    assert {actor_role.role_id, affected_role.role_id} <= set(lock_args["role_ids"])
    assert {
        old_parent.dept_id,
        new_parent.dept_id,
        source.dept_id,
        child.dept_id,
    } <= set(lock_args["dept_ids"])
    assert {actor.user_id, affected.user_id} <= set(lock_args["user_ids"])
    assert source.parent_id == new_parent.dept_id
    assert child.ancestors == (
        f"{new_parent.ancestors},{new_parent.dept_id},{source.dept_id}"
    )


async def test_move_does_not_match_unrelated_ancestor_id_prefix(
    db_session: AsyncSession,
) -> None:
    super_role = await db_session.scalar(
        select(Role).where(
            Role.tenant_id == 0,
            Role.role_code == SUPER_ADMIN_ROLE_CODE,
        )
    )
    assert super_role is not None
    actor = _user("phase3-prefix-super", [super_role])
    source_id = 1_200_000_000_000_012
    unrelated_id = 12_000_000_000_000_123
    destination = _department("phase3-prefix-destination")
    source = Dept(
        tenant_id=0,
        dept_id=source_id,
        parent_id=None,
        ancestors="0",
        dept_name=f"phase3-prefix-source-{source_id}",
        order_num=0,
        status=STATUS_ENABLED,
    )
    source_child = Dept(
        tenant_id=0,
        dept_id=next_id(),
        parent_id=source_id,
        ancestors=f"0,{source_id}",
        dept_name=f"phase3-prefix-source-child-{next_id()}",
        order_num=0,
        status=STATUS_ENABLED,
    )
    unrelated = Dept(
        tenant_id=0,
        dept_id=unrelated_id,
        parent_id=None,
        ancestors="0",
        dept_name=f"phase3-prefix-unrelated-{unrelated_id}",
        order_num=0,
        status=STATUS_ENABLED,
    )
    unrelated_child = Dept(
        tenant_id=0,
        dept_id=next_id(),
        parent_id=unrelated_id,
        ancestors=f"0,{unrelated_id}",
        dept_name=f"phase3-prefix-unrelated-child-{next_id()}",
        order_num=0,
        status=STATUS_ENABLED,
    )
    await _persist_graph(
        db_session, actor, destination, source, source_child, unrelated, unrelated_child
    )

    await dept_service.move(
        db_session,
        dept_id=source_id,
        new_parent_id=destination.dept_id,
        actor_user_id=actor.user_id,
        tenant=_tenant(actor),
    )

    assert source_child.ancestors == f"0,{destination.dept_id},{source_id}"
    assert unrelated_child.ancestors == f"0,{unrelated_id}"


async def test_move_rejects_member_phantom_discovered_after_global_lock(
    db_session: AsyncSession,
) -> None:
    actor_root = _department("phase3-stale-root")
    old_parent = _department("phase3-stale-old", parent=actor_root)
    new_parent = _department("phase3-stale-new", parent=actor_root)
    source = _department("phase3-stale-source", parent=old_parent)
    actor_role = _role(
        f"R_PHASE3_STALE_ACTOR_{next_id()}",
        data_scope=DATA_SCOPE_DEPT_AND_SUB,
        permissions=("system:dept:move", "system:dept:list"),
    )
    affected_role = _role(
        f"R_PHASE3_STALE_AFFECTED_{next_id()}",
        data_scope=DATA_SCOPE_DEPT_AND_SUB,
    )
    actor = _user("phase3-stale-actor", [actor_role])
    existing_member = _user("phase3-stale-existing", [affected_role])
    late_member = _user("phase3-stale-late", [])
    await _persist_graph(
        db_session,
        actor_root,
        old_parent,
        new_parent,
        source,
        actor_role,
        affected_role,
        actor,
        existing_member,
        late_member,
    )
    await _bind_department(db_session, actor, actor_root, primary=True)
    await _bind_department(db_session, existing_member, old_parent, primary=True)
    original_lock = authorization_lock_service.lock_targets

    async def lock_and_add_member(*args, **kwargs):  # noqa: ANN002, ANN003
        locked = await original_lock(*args, **kwargs)
        await db_session.execute(
            insert(user_roles).values(
                tenant_id=0,
                user_id=late_member.user_id,
                role_id=affected_role.role_id,
            )
        )
        return locked

    move = getattr(dept_service, "move", None)
    assert move is not None
    with (
        patch.object(
            authorization_lock_service,
            "lock_targets",
            side_effect=lock_and_add_member,
        ) as lock_mock,
        pytest.raises(BusinessException) as exc_info,
    ):
        await move(
            db_session,
            dept_id=source.dept_id,
            new_parent_id=new_parent.dept_id,
            actor_user_id=actor.user_id,
            expected_snapshot=None,
            tenant=_tenant(actor),
        )

    assert exc_info.value.error_code == "AUTHORIZATION_SNAPSHOT_STALE"
    assert lock_mock.await_count == 1
    assert source.parent_id == old_parent.dept_id
