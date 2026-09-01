"""User API transaction ownership and separated-writer tests."""

import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.routing import APIRoute

from app.modules.system.api.user import (
    add_user,
    update_user,
    update_user_departments,
    update_user_roles,
)
from app.modules.system.api.user import (
    router as user_router,
)
from app.modules.system.schemas.user import (
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


async def test_user_create_assigns_roles_after_generated_id_and_departments() -> None:
    tenant = tenant_context(actor_user_id=42)
    user_in = UserCreate(
        user_name="e2ecreate",
        nickname="E2E Create",
        password="E2ePass123",
        status="1",
        dept_ids=[UserDeptItem(dept_id="1", is_primary=True)],
    )
    created_user = SimpleNamespace(user_id=123)
    db_mock = AsyncMock()
    calls: list[str] = []

    async def flush() -> None:
        calls.append("flush")

    async def assign_departments(*_args, **_kwargs) -> None:
        calls.append("departments")

    async def assign_roles(*_args, **_kwargs) -> None:
        calls.append("roles")

    db_mock.flush.side_effect = flush
    with (
        patch(
            "app.modules.system.api.user.user_role_assignment_service.ensure_create_permissions",
            new=AsyncMock(),
        ) as ensure_permissions,
        patch(
            "app.modules.system.api.user.user_service.create_user",
            new=AsyncMock(return_value=created_user),
        ),
        patch(
            "app.modules.system.api.user.user_department_assignment_service."
            "ensure_create_permissions",
            new=AsyncMock(),
        ) as ensure_department_permissions,
        patch(
            "app.modules.system.api.user.user_role_assignment_service.assign_created_user_roles",
            new=AsyncMock(side_effect=assign_roles),
        ) as assign_created_roles,
        patch(
            "app.modules.system.api.user.user_department_assignment_service."
            "assign_created_user_departments",
            new=AsyncMock(side_effect=assign_departments),
        ) as assign_created_departments,
    ):
        await add_user(
            user_in=user_in,
            db=db_mock,
            current_user=_actor(),
            tenant=tenant,
        )

    ensure_permissions.assert_awaited_once_with(
        db_mock,
        actor_user_id=42,
        explicit_roles=False,
        tenant=tenant,
    )
    ensure_department_permissions.assert_awaited_once_with(
        db_mock,
        actor_user_id=42,
        has_departments=True,
        tenant=tenant,
    )
    assert calls == ["flush", "roles", "departments"]
    assign_created_roles.assert_awaited_once_with(
        db_mock,
        actor_user_id=42,
        target_user_id=123,
        role_ids=None,
        dept_ids=[1],
        tenant=tenant,
    )
    assign_created_departments.assert_awaited_once_with(
        db_mock,
        actor_user_id=42,
        target_user_id=123,
        dept_assignments=[(1, True)],
        tenant=tenant,
    )
    db_mock.commit.assert_awaited_once()


async def test_profile_update_uses_only_the_profile_writer() -> None:
    tenant = tenant_context(actor_user_id=42)
    user_in = UserUpdate(user_name="alice", status="1")
    db_mock = AsyncMock()

    with patch(
        "app.modules.system.api.user.user_service.update_user",
        new=AsyncMock(),
    ) as update_profile:
        await update_user(user_id=123, user_in=user_in, db=db_mock, tenant=tenant)

    update_profile.assert_awaited_once_with(db_mock, 123, user_in, tenant=tenant)
    db_mock.commit.assert_awaited_once()


async def test_role_update_commits_only_after_shared_policy_succeeds() -> None:
    tenant = tenant_context(actor_user_id=42)
    body = UserRoleUpdate(role_ids=["11", "12"])
    db_mock = AsyncMock()

    with patch(
        "app.modules.system.api.user.user_role_assignment_service.replace_roles",
        new=AsyncMock(),
    ) as replace_roles:
        await update_user_roles(
            user_id=123,
            body=body,
            db=db_mock,
            current_user=_actor(),
            tenant=tenant,
        )

    replace_roles.assert_awaited_once_with(
        db_mock,
        actor_user_id=42,
        target_user_id=123,
        role_ids=["11", "12"],
        tenant=tenant,
    )
    db_mock.commit.assert_awaited_once()


async def test_department_update_commits_only_after_shared_policy_succeeds() -> None:
    tenant = tenant_context(actor_user_id=42)
    body = UserDepartmentUpdate(
        dept_assignments=[
            UserDepartmentAssignment(dept_id="21", is_primary=True),
            UserDepartmentAssignment(dept_id="22", is_primary=False),
        ]
    )
    db_mock = AsyncMock()

    with patch(
        "app.modules.system.api.user.user_department_assignment_service."
        "replace_departments",
        new=AsyncMock(),
    ) as replace_departments:
        await update_user_departments(
            user_id=123,
            body=body,
            db=db_mock,
            current_user=_actor(),
            tenant=tenant,
        )

    replace_departments.assert_awaited_once_with(
        db_mock,
        actor_user_id=42,
        target_user_id=123,
        dept_assignments=[(21, True), (22, False)],
        tenant=tenant,
    )
    db_mock.commit.assert_awaited_once()


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
