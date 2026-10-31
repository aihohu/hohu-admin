"""Control-plane identities retain their tenant and current security version."""

import pytest
from sqlalchemy import update

from app.core.authorization_plan import ResourceKind
from app.core.exceptions import BusinessException
from app.core.tenant import TenantContext
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service.authorization_control_identity import (
    assert_identity,
    read_identity,
)
from tests.modules.system.test_authorization_system_candidate import (
    TENANT,
)
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


async def test_identity_is_current_and_supplies_complete_typed_keys(system_sessions):
    factory, _ = system_sessions
    async with factory() as db:
        identity = await read_identity(
            db, tenant=TENANT, actor_user_version=1, system=True
        )
        assert identity.tenant == TENANT
        assert identity.system_admin is True
        assert any(key.kind is ResourceKind.USER for key in identity.keys)
        assert any(key.kind is ResourceKind.TENANT for key in identity.keys)
        assert await assert_identity(db, identity) == identity


@pytest.mark.parametrize("version", [True, 0, 2, "1"])
async def test_identity_refuses_untrusted_or_stale_versions(system_sessions, version):
    factory, _ = system_sessions
    async with factory() as db:
        with pytest.raises(BusinessException):
            await read_identity(
                db, tenant=TENANT, actor_user_version=version, system=True
            )


async def test_identity_revalidation_detects_revocation(system_sessions):
    factory, plain = system_sessions
    async with factory() as db:
        identity = await read_identity(
            db, tenant=TENANT, actor_user_version=1, system=True
        )
        async with plain() as writer:
            await writer.execute(
                update(Role).where(Role.role_id == 201).values(status="2")
            )
            await writer.commit()
        with pytest.raises(BusinessException):
            await assert_identity(db, identity)


async def test_identity_refuses_disabled_user_and_cross_tenant(system_sessions):
    factory, plain = system_sessions
    async with plain() as writer:
        await writer.execute(update(User).where(User.user_id == 101).values(status="2"))
        await writer.commit()
    async with factory() as db:
        with pytest.raises(BusinessException):
            await read_identity(db, tenant=TENANT, actor_user_version=1)
    async with factory() as db:
        with pytest.raises(BusinessException):
            await read_identity(
                db,
                tenant=TenantContext(1, "plan-1", 101, 1, "access_token"),
                actor_user_version=1,
                system=True,
            )
