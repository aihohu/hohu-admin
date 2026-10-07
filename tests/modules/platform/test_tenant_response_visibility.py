"""A platform success response exposes committed state to a separate login session."""

from types import SimpleNamespace

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, select

from app.core.config import settings
from app.core.exceptions import AuthenticationException, setup_exception_handlers
from app.core.id_generator import next_id
from app.core.security import get_password_hash
from app.db.session import AsyncSessionLocal, engine
from app.modules.auth.schemas.auth import LoginCredentials
from app.modules.auth.service import auth_service, get_current_user
from app.modules.platform.api import control_router
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User


async def test_activation_and_disable_are_visible_when_response_starts(monkeypatch):
    tenant_id, user_id, actor_id = next_id(), next_id(), next_id()
    tenant_code = f"boundary-{tenant_id}"
    credentials = LoginCredentials(
        tenant_code=tenant_code,
        user_name="admin",
        password="Boundary123456",
    )
    monkeypatch.setattr(settings, "TENANT_MODE", "hosted")
    monkeypatch.setattr(settings, "TENANT_HOSTED_LOGIN_ENABLED", True)
    test_app = FastAPI()
    setup_exception_handlers(test_app)
    test_app.include_router(control_router, prefix="/platform")
    test_app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        tenant_id=0,
        user_id=actor_id,
        user_name="boundary-owner",
        status="1",
        roles=[SimpleNamespace(tenant_id=0, role_code="R_SUPER", status="1")],
    )
    observations = []

    async def observed_app(scope, receive, send):
        async def observe(message):
            if message["type"] == "http.response.start":
                async with AsyncSessionLocal() as reader:
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
                    completions = list(
                        (
                            await reader.scalars(
                                select(SysOperationLog).where(
                                    SysOperationLog.user_id == actor_id,
                                    SysOperationLog.action == "completed",
                                )
                            )
                        ).all()
                    )
                    observations.append(
                        (message["status"], login_result, len(completions))
                    )
            await send(message)

        await test_app(scope, receive, observe)

    try:
        async with AsyncSessionLocal() as setup:
            setup.add(
                Tenant(
                    tenant_id=tenant_id,
                    tenant_code=tenant_code,
                    tenant_name="Boundary test",
                    status="2",
                    lifecycle_state="prepared",
                    bootstrap_version=1,
                    bootstrap_key_hash=f"{tenant_id:064x}",
                    bootstrap_fingerprint="f" * 64,
                    row_version=1,
                )
            )
            await setup.flush()
            setup.add(
                User(
                    user_id=user_id,
                    tenant_id=tenant_id,
                    user_name="admin",
                    hashed_password=get_password_hash(credentials.password),
                    status="1",
                )
            )
            await setup.commit()
        async with AsyncClient(
            transport=ASGITransport(app=observed_app), base_url="http://test"
        ) as client:
            for action in ("activate", "disable"):
                response = await client.post(
                    f"/platform/tenants/{tenant_id}/{action}",
                    headers={
                        "X-Platform-Reason": "Response visibility test",
                        "X-Platform-Ticket": "BOUNDARY-1",
                        "X-Correlation-ID": f"boundary-{action}-{tenant_id}",
                    },
                )
                assert response.status_code == 200
        assert observations == [(200, user_id, 1), (200, "INVALID_CREDENTIALS", 2)]
    finally:
        async with AsyncSessionLocal() as cleanup:
            await cleanup.execute(
                delete(SysOperationLog).where(SysOperationLog.user_id == actor_id)
            )
            await cleanup.execute(delete(User).where(User.tenant_id == tenant_id))
            await cleanup.execute(delete(Tenant).where(Tenant.tenant_id == tenant_id))
            await cleanup.commit()
        await engine.dispose()
