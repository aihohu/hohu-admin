"""Security-sensitive user mutations revoke all previously issued sessions."""

from unittest.mock import AsyncMock, MagicMock, patch

from app.modules.system.models.user import User
from app.modules.system.schemas.user import ChangePassword, ResetPassword, UserUpdate
from app.modules.system.service.user_service import user_service
from tests.tenant_helpers import tenant_context


def _result(user: User):
    result = MagicMock()
    result.scalars.return_value.first.return_value = user
    return result


def _user(*, status: str = "1", auth_version: int = 4) -> User:
    return User(
        user_id=123,
        tenant_id=0,
        user_name="alice",
        hashed_password="old-hash",
        status=status,
        auth_version=auth_version,
    )


async def test_status_transition_bumps_auth_version_but_profile_edit_does_not() -> None:
    tenant = tenant_context(actor_user_id=42)
    user = _user()
    db = AsyncMock()
    db.execute = AsyncMock(return_value=_result(user))

    with patch("app.modules.system.service.user_service.redis_client", AsyncMock()):
        await user_service.update_user(
            db,
            user.user_id,
            UserUpdate(user_name="alice", nickname="Alice", status="1"),
            tenant=tenant,
        )
        assert user.auth_version == 4

        await user_service.update_user(
            db,
            user.user_id,
            UserUpdate(user_name="alice", nickname="Alice", status="2"),
            tenant=tenant,
        )

    assert user.auth_version == 5
    db.commit.assert_not_awaited()


async def test_password_mutations_and_explicit_revoke_bump_auth_version() -> None:
    tenant = tenant_context(actor_user_id=42)
    user = _user()
    db = AsyncMock()
    db.scalar = AsyncMock(return_value=user)

    with (
        patch(
            "app.modules.system.service.user_service.verify_password",
            return_value=True,
        ),
        patch(
            "app.modules.system.service.user_service.get_password_hash",
            return_value="new-hash",
        ),
    ):
        await user_service.change_password(
            db,
            user.user_id,
            ChangePassword(old_password="old-password", new_password="NewPass123"),
            tenant=tenant,
        )
        assert user.auth_version == 5

        await user_service.reset_password(
            db,
            user.user_id,
            ResetPassword(new_password="OtherPass123"),
            tenant=tenant,
        )
        assert user.auth_version == 6

        await user_service.revoke_sessions(db, user.user_id, tenant=tenant)

    assert user.auth_version == 7
    assert user.hashed_password == "new-hash"
    db.commit.assert_not_awaited()
