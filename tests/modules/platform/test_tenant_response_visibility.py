"""Success at response.start is already visible to an independent login session."""

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update

from app.core.exceptions import AuthenticationException
from app.core.security import get_password_hash
from app.modules.auth.schemas.auth import LoginCredentials
from app.modules.auth.service import auth_service
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User
from tests.modules.platform.test_authorization_tenant_http import (
    catalog_http as catalog_http,
)
from tests.modules.platform.test_authorization_tenant_http import (
    catalog_sessions as catalog_sessions,
)
from tests.modules.platform.test_authorization_tenant_http import (
    http_system as http_system,
)
from tests.modules.platform.test_authorization_tenant_http import (
    plan_sessions as plan_sessions,
)
from tests.modules.platform.test_authorization_tenant_http import (
    system_sessions as system_sessions,
)
from tests.modules.platform.test_authorization_tenant_http import (
    tenant_http as tenant_http,
)


async def test_activation_and_disable_are_visible_when_response_starts(tenant_http):
    original, plain = tenant_http
    credentials = LoginCredentials(
        tenant_code="plan-1", user_name="planner", password="Boundary123456"
    )
    async with plain() as db:
        await db.execute(
            update(Tenant)
            .where(Tenant.tenant_id == 1)
            .values(status="2", lifecycle_state="prepared")
        )
        await db.execute(
            update(User)
            .where(User.user_id == 102)
            .values(hashed_password=get_password_hash(credentials.password))
        )
        await db.commit()
    observations = []

    async def observed_app(scope, receive, send):
        async def observe(message):
            if message["type"] == "http.response.start":
                async with plain() as reader:
                    try:
                        tenant = await auth_service.resolve_login_tenant(
                            credentials, reader
                        )
                        user = await auth_service._verify_password_login(
                            credentials, reader, tenant=tenant
                        )
                        login_result = user.user_id
                    except AuthenticationException as exc:
                        login_result = exc.error_code
                    completions = (
                        (
                            await reader.execute(
                                select(SysOperationLog).where(
                                    SysOperationLog.action == "completed"
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    observations.append(
                        (message["status"], login_result, len(completions))
                    )
            await send(message)

        await original._transport.app(scope, receive, observe)

    async with AsyncClient(
        transport=ASGITransport(app=observed_app),
        base_url="http://test",
        headers=original.headers,
    ) as client:
        for action in ("activate", "disable"):
            response = await client.post(f"/platform/tenants/1/{action}")
            assert response.status_code == 200, response.text
    assert observations == [(200, 102, 1), (200, "INVALID_CREDENTIALS", 2)]
