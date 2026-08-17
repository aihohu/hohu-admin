"""User API transaction ownership and separated-writer tests."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.modules.system.api.user import add_user, update_user, update_user_roles
from app.modules.system.schemas.user import (
    UserCreate,
    UserDeptItem,
    UserRoleUpdate,
    UserUpdate,
)


def _actor() -> SimpleNamespace:
    return SimpleNamespace(user_id=42)


async def test_user_create_assigns_roles_after_generated_id_and_departments() -> None:
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

    async def update_departments(*_args, **_kwargs) -> None:
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
            "app.modules.system.api.user.config_service.get_bool",
            new=AsyncMock(return_value=False),
        ),
        patch(
            "app.modules.system.api.user.dept_service.update_user_depts",
            new=AsyncMock(side_effect=update_departments),
        ),
        patch(
            "app.modules.system.api.user.user_role_assignment_service.assign_created_user_roles",
            new=AsyncMock(side_effect=assign_roles),
        ) as assign_created_roles,
    ):
        await add_user(user_in=user_in, db=db_mock, current_user=_actor())

    ensure_permissions.assert_awaited_once_with(
        db_mock,
        actor_user_id=42,
        explicit_roles=False,
    )
    assert calls == ["flush", "roles", "departments"]
    assign_created_roles.assert_awaited_once_with(
        db_mock,
        actor_user_id=42,
        target_user_id=123,
        role_ids=None,
        dept_ids=[1],
    )
    db_mock.commit.assert_awaited_once()


async def test_profile_update_uses_only_the_profile_writer() -> None:
    user_in = UserUpdate(user_name="alice", status="1")
    db_mock = AsyncMock()

    with patch(
        "app.modules.system.api.user.user_service.update_user",
        new=AsyncMock(),
    ) as update_profile:
        await update_user(user_id=123, user_in=user_in, db=db_mock)

    update_profile.assert_awaited_once_with(db_mock, 123, user_in)
    db_mock.commit.assert_awaited_once()


async def test_role_update_commits_only_after_shared_policy_succeeds() -> None:
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
        )

    replace_roles.assert_awaited_once_with(
        db_mock,
        actor_user_id=42,
        target_user_id=123,
        role_ids=["11", "12"],
    )
    db_mock.commit.assert_awaited_once()
