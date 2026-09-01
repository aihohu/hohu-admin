"""Tenant-scoped reads and append-only boundaries for System audit logs."""

from app.core.id_generator import next_id
from app.modules.system.models.login_log import SysLoginLog
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.schemas.login_log import LoginLogQuery
from app.modules.system.schemas.operation_log import OperationLogQuery
from app.modules.system.service.login_log_service import login_log_service
from app.modules.system.service.operation_log_service import operation_log_service
from tests.tenant_helpers import create_test_tenant, tenant_context


async def test_audit_lists_do_not_leak_and_tenant_services_are_append_only(db_session):
    tenant_b = await create_test_tenant(db_session, prefix="audit-b")
    marker = next_id()
    operation_a = SysOperationLog(
        tenant_id=0,
        audit_scope="tenant",
        user_id=marker,
        username="tenant-a-auditor",
        module="system",
        action="update",
        method="PUT",
        path="/system/user",
        status_code=200,
    )
    operation_b = SysOperationLog(
        tenant_id=tenant_b.tenant_id,
        audit_scope="tenant",
        user_id=marker + 1,
        username="tenant-b-auditor",
        module="system",
        action="update",
        method="PUT",
        path="/system/user",
        status_code=200,
    )
    login_a = SysLoginLog(
        tenant_id=0,
        audit_scope="tenant",
        user_id=marker,
        username="tenant-a-auditor",
        status="1",
    )
    unresolved = SysLoginLog(
        tenant_id=None,
        audit_scope="unresolved",
        user_id=None,
        username="unknown",
        status="2",
    )
    db_session.add_all([operation_a, operation_b, login_a, unresolved])
    await db_session.flush()

    tenant_a_ctx = tenant_context(actor_user_id=marker)
    operation_page = await operation_log_service.get_list(
        db_session, OperationLogQuery(), tenant=tenant_a_ctx
    )
    login_page = await login_log_service.get_list(
        db_session, LoginLogQuery(), tenant=tenant_a_ctx
    )

    assert operation_a in operation_page.records
    assert operation_b not in operation_page.records
    assert login_a in login_page.records
    assert unresolved not in login_page.records
    assert not hasattr(operation_log_service, "batch_delete")
    assert not hasattr(operation_log_service, "clean")
    assert not hasattr(login_log_service, "batch_delete")
    assert not hasattr(login_log_service, "clean")
