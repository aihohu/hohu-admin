"""User API transaction ownership and separated-writer tests."""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.routing import APIRoute

from app.modules.system.api.user import (
    add_user,
    change_password,
    revoke_user_sessions,
    update_user,
    update_user_departments,
    update_user_roles,
)
from app.modules.system.api.user import (
    router as user_router,
)
from app.modules.system.schemas.user import (
    ChangePassword,
    UserCreate,
    UserDepartmentAssignment,
    UserDepartmentUpdate,
    UserDeptItem,
    UserRoleUpdate,
    UserUpdate,
)
from tests.tenant_helpers import tenant_context


def _actor() -> SimpleNamespace:
    return SimpleNamespace(user_id=42)


async def test_user_create_passes_one_complete_input_to_owner() -> None:
    tenant, actor, db = tenant_context(actor_user_id=42), _actor(), AsyncMock()
    payload = UserCreate(
        user_name="e2ecreate",
        password="E2ePass123",
        status="1",
        dept_ids=[UserDeptItem(dept_id="1", is_primary=True)],
    )
    with patch("app.modules.system.api.user.system_write", new=AsyncMock()) as owner:
        await add_user(user_in=payload, db=db, current_user=actor, tenant=tenant)
    owner.assert_awaited_once_with(
        db, actor, operation="user.create", payload=payload, tenant=tenant
    )
    db.commit.assert_not_awaited()
    db.flush.assert_not_awaited()


async def test_profile_update_uses_only_the_profile_writer() -> None:
    tenant, actor, db = tenant_context(actor_user_id=42), _actor(), AsyncMock()
    payload = UserUpdate(user_name="alice", status="1")
    with patch("app.modules.system.api.user.system_write", new=AsyncMock()) as owner:
        await update_user(
            user_id=123, user_in=payload, db=db, current_user=actor, tenant=tenant
        )
    owner.assert_awaited_once_with(
        db,
        actor,
        operation="user.update",
        payload=payload,
        target_id=123,
        tenant=tenant,
    )
    db.commit.assert_not_awaited()


async def test_change_password_delegates_completion_to_owner() -> None:
    tenant, actor, db = tenant_context(actor_user_id=42), _actor(), AsyncMock()
    body = ChangePassword(old_password="old-password", new_password="NewPass123")
    with patch("app.modules.system.api.user.system_write", new=AsyncMock()) as owner:
        await change_password(body=body, current_user=actor, db=db, tenant=tenant)
    owner.assert_awaited_once_with(
        db, actor, operation="user.change_password", payload=body, tenant=tenant
    )
    db.commit.assert_not_awaited()


async def test_explicit_session_revoke_delegates_completion_to_owner() -> None:
    tenant, actor, db = tenant_context(actor_user_id=42), _actor(), AsyncMock()
    with patch("app.modules.system.api.user.system_write", new=AsyncMock()) as owner:
        await revoke_user_sessions(
            user_id=123, current_user=actor, db=db, tenant=tenant
        )
    owner.assert_awaited_once_with(
        db, actor, operation="user.revoke", target_id=123, tenant=tenant
    )
    db.commit.assert_not_awaited()


async def test_role_update_preserves_complete_assignment_for_owner() -> None:
    tenant, actor, db = tenant_context(actor_user_id=42), _actor(), AsyncMock()
    body = UserRoleUpdate(role_ids=["11", "12"])
    with patch("app.modules.system.api.user.system_write", new=AsyncMock()) as owner:
        await update_user_roles(
            user_id=123, body=body, db=db, current_user=actor, tenant=tenant
        )
    owner.assert_awaited_once_with(
        db,
        actor,
        operation="user.roles",
        payload=["11", "12"],
        target_id=123,
        tenant=tenant,
    )
    db.commit.assert_not_awaited()


async def test_department_update_preserves_complete_assignment_for_owner() -> None:
    tenant, actor, db = tenant_context(actor_user_id=42), _actor(), AsyncMock()
    body = UserDepartmentUpdate(
        dept_assignments=[
            UserDepartmentAssignment(dept_id="21", is_primary=True),
            UserDepartmentAssignment(dept_id="22", is_primary=False),
        ]
    )
    with patch("app.modules.system.api.user.system_write", new=AsyncMock()) as owner:
        await update_user_departments(
            user_id=123, body=body, db=db, current_user=actor, tenant=tenant
        )
    owner.assert_awaited_once_with(
        db,
        actor,
        operation="user.departments",
        payload=[(21, True), (22, False)],
        target_id=123,
        tenant=tenant,
    )
    db.commit.assert_not_awaited()


def test_department_update_route_requires_edit_and_department_list() -> None:
    route = next(
        route
        for route in user_router.routes
        if isinstance(route, APIRoute)
        and route.path == "/{user_id}/departments"
        and "PUT" in route.methods
    )
    permission_codes = {
        inspect.getclosurevars(dependency.call).nonlocals.get("perm_code")
        for dependency in route.dependant.dependencies
    }

    assert {"system:user:edit", "system:dept:list"} <= permission_codes
