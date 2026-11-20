"""The write identity retains the verified token version, not mutable ORM state."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest

from app.core.authorization_plan import LockPlan, ResourceKey, ResourceKind, TenantLock
from app.core.exceptions import AuthenticationException, BusinessException
from app.core.security import create_access_token
from app.modules.auth import service
from app.modules.system.models.user import User
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
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


@pytest.mark.parametrize("locked", [False, True])
async def test_background_projection_requires_locks_and_never_issues_jwt_identity(
    system_sessions, locked
):
    factory, _ = system_sessions
    tenant = replace(TENANT, source="platform_control")
    async with factory() as db:
        async with authorization_transactions.scope(db, tenants=(TenantLock(0),)) as tx:
            if not locked:
                with pytest.raises(BusinessException):
                    await service.load_background_projection_authority(
                        db, tenant=tenant, tx=tx
                    )
                return
            await tx.lock(
                LockPlan(
                    existing=(
                        ResourceKey(ResourceKind.TENANT, None, 0),
                        ResourceKey(ResourceKind.USER, 0, 101),
                    )
                )
            )
            user = await service.load_background_projection_authority(
                db, tenant=tenant, tx=tx
            )
            assert user._tenant_context.source == "platform_control"
            with pytest.raises(AuthenticationException):
                service.access_identity(user)
