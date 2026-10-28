"""The write identity retains the verified token version, not mutable ORM state."""

from unittest.mock import AsyncMock

import pytest

from app.core.exceptions import AuthenticationException
from app.core.security import create_access_token
from app.modules.auth import service
from app.modules.system.models.user import User
from tests.modules.system.test_authorization_system_candidate import TENANT
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


async def test_http_auth_retains_signed_version_after_orm_refresh(
    system_sessions, monkeypatch
):
    _, plain = system_sessions
    monkeypatch.setattr(service, "_is_blacklisted", AsyncMock(return_value=False))
    token = create_access_token(
        subject=str(TENANT.actor_user_id), tenant_id=0, tenant_version=1, user_version=1
    )
    async with plain() as db:
        user = await service.get_current_user(token, db)
        user.auth_version = 2
        identity = service.access_identity(user)
        assert identity.user_version == 1
        assert identity.tenant == TENANT
        await db.rollback()


async def test_bare_orm_user_cannot_supply_write_identity(system_sessions):
    _, plain = system_sessions
    async with plain() as db:
        user = await db.get(User, TENANT.actor_user_id)
        with pytest.raises(AuthenticationException):
            service.access_identity(user)
