import pytest

from app.core.exceptions import BusinessException
from app.core.id_generator import next_id
from app.core.tenant import PlatformContext
from app.modules.platform.constants import PLATFORM_SUPPORT_READ
from app.modules.system.models.login_log import SysLoginLog
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.models.tenant import Tenant
from app.modules.system.service.tenant_support_service import tenant_support_service


def _support_context(tenant_id: int) -> PlatformContext:
    return PlatformContext(
        actor_principal_id=92,
        actor_name="support-reader",
        principal_type="human",
        permissions=frozenset({PLATFORM_SUPPORT_READ}),
        reason="Investigate tenant audit timeline",
        ticket_id="SUPPORT-92",
        correlation_id=f"support-92:{tenant_id}",
        target_tenant_id=tenant_id,
    )


async def test_support_queries_return_minimized_target_tenant_projection(db_session):
    tenant_id = next_id()
    other_tenant_id = next_id()
    db_session.add_all(
        [
            Tenant(
                tenant_id=tenant_id,
                tenant_code=f"support-{tenant_id}",
                tenant_name="Support Target",
                status="2",
                lifecycle_state="prepared",
            ),
            Tenant(
                tenant_id=other_tenant_id,
                tenant_code=f"support-{other_tenant_id}",
                tenant_name="Other Target",
                status="2",
                lifecycle_state="prepared",
            ),
        ]
    )
    await db_session.flush()
    secret = "token=abcdefghijklmnop123456"
    db_session.add_all(
        [
            SysOperationLog(
                tenant_id=tenant_id,
                audit_scope="tenant",
                user_id=991,
                username="private-user",
                module="system",
                action="update",
                method="POST",
                path=f"/system/user/{secret}",
                request_params=secret,
                status_code=200,
                ip="203.0.113.9",
                duration=12,
            ),
            SysOperationLog(
                tenant_id=other_tenant_id,
                audit_scope="tenant",
                user_id=992,
                username="other-user",
                module="system",
                action="delete",
                method="DELETE",
                path="/system/user/2",
                status_code=200,
            ),
            SysLoginLog(
                tenant_id=tenant_id,
                audit_scope="tenant",
                user_id=991,
                username="private-user",
                ip="203.0.113.9",
                user_agent=secret,
                status="2",
                message="凭据无效",
            ),
        ]
    )
    await db_session.flush()

    operation_page = await tenant_support_service.list_operation_logs(
        db_session,
        tenant_id=tenant_id,
        current=1,
        size=20,
        platform=_support_context(tenant_id),
    )
    login_page = await tenant_support_service.list_login_logs(
        db_session,
        tenant_id=tenant_id,
        current=1,
        size=20,
        platform=_support_context(tenant_id),
    )

    assert operation_page.total == 1
    assert login_page.total == 1
    operation = operation_page.records[0]
    login = login_page.records[0]
    assert operation.category == "system"
    assert operation.event_type == "update"
    assert login.category == "authentication"
    assert login.event_type == "login_failed"
    serialized = str(operation_page) + str(login_page)
    for private_value in (
        "private-user",
        "203.0.113.9",
        secret,
        "/system/user/",
    ):
        assert private_value not in serialized


async def test_support_query_requires_existing_target_tenant(db_session):
    missing_id = next_id()
    with pytest.raises(BusinessException) as exc_info:
        await tenant_support_service.list_operation_logs(
            db_session,
            tenant_id=missing_id,
            current=1,
            size=20,
            platform=_support_context(missing_id),
        )

    assert exc_info.value.error_code == "PLATFORM_TENANT_NOT_FOUND"
