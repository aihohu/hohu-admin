"""Public account hints have an independent, current, four-key boundary."""

import pytest
from sqlalchemy import insert, update

from app.core.exceptions import BusinessException
from app.core.tenant import TenantContext, TenantLocatorContext
from app.db.authorization_runtime import AuthorizationRuntime
from app.modules.system.models.mail_config import MailConfig
from app.modules.system.models.setting import SystemSetting
from app.modules.system.models.tenant import Tenant
from tests.modules.system.test_authorization_system_candidate import (
    system_sessions as system_sessions,
)
from tests.modules.system.test_authorization_transaction_barriers import (
    plan_sessions as plan_sessions,
)


@pytest.mark.parametrize("tenant_id", [0, 1])
async def test_public_capability_snapshot_is_exact_and_tenant_scoped(
    system_sessions, tenant_id
):
    _, plain = system_sessions
    async with plain() as db:
        await db.execute(
            insert(MailConfig).values(
                config_id=1,
                values={
                    "enabled": True,
                    "host": "mail.invalid",
                    "from_email": "sender@example.com",
                },
            )
        )
        for tid in (0, 1):
            for key, value in {
                "register_enabled": "true" if tid == 0 else "false",
                "password_reset_enabled": "true",
                "email_code_minutes": str(5 + tid),
                "auth:default_password": "NeverDeliverThis123!",
            }.items():
                await db.execute(
                    insert(SystemSetting).values(
                        tenant_id=tid,
                        setting_key=key,
                        setting_value=value,
                        status="1",
                    )
                )
        await db.commit()
    result = await AuthorizationRuntime(plain.kw["bind"]).parameter_read(
        tenant=TenantLocatorContext(tenant_id, f"plan-{tenant_id}", 1),
        actor_user_version=None,
        operation="auth.capabilities",
    )
    assert result == {
        "registrationEnabled": tenant_id == 0,
        "passwordResetEnabled": True,
        "emailEnabled": True,
        "codeMinutes": 5 + tenant_id,
    }


@pytest.mark.parametrize("invalid", ["version", "code", "disabled", "actor"])
async def test_public_capability_rejects_stale_or_authenticated_locator(
    system_sessions, invalid
):
    _, plain = system_sessions
    tenant = TenantLocatorContext(
        0,
        "wrong-code" if invalid == "code" else "plan-0",
        2 if invalid == "version" else 1,
    )
    if invalid == "disabled":
        async with plain() as db:
            await db.execute(
                update(Tenant)
                .where(Tenant.tenant_id == 0)
                .values(status="2", lifecycle_state="disabled")
            )
            await db.commit()
    if invalid == "actor":
        tenant = TenantContext(0, "plan-0", 101, 1, "access_token")
    with pytest.raises(BusinessException):
        await AuthorizationRuntime(plain.kw["bind"]).parameter_read(
            tenant=tenant,
            actor_user_version=None,
            operation="auth.capabilities",
        )
