from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, insert, update
from sqlalchemy.exc import DBAPIError

from app.core.exceptions import BusinessException
from app.core.security import create_platform_access_token, get_password_hash
from app.modules.platform.audit import add_platform_completion
from app.modules.platform.auth import authenticate_platform_token
from app.modules.platform.constants import PLATFORM_AI_READ, PLATFORM_AI_WRITE
from app.modules.platform.models import PlatformAuditLog, PlatformPrincipal


async def test_platform_security_changes_automatically_revoke_old_versions(db_session):
    principal = PlatformPrincipal(
        principal_name="test_platform_revocation",
        display_name="Test Platform Revocation",
        hashed_password=get_password_hash("a-long-test-password1"),
        permissions=[PLATFORM_AI_READ],
    )
    db_session.add(principal)
    await db_session.flush()
    assert principal.row_version == 1
    old_token = create_platform_access_token(
        subject=str(principal.principal_id), principal_version=principal.row_version
    )

    await db_session.execute(
        update(PlatformPrincipal)
        .where(PlatformPrincipal.principal_id == principal.principal_id)
        .values(status="2")
    )
    await db_session.refresh(principal)
    assert principal.row_version == 2

    await db_session.execute(
        update(PlatformPrincipal)
        .where(PlatformPrincipal.principal_id == principal.principal_id)
        .values(status="1")
    )
    await db_session.refresh(principal)
    assert principal.row_version == 3

    await db_session.execute(
        update(PlatformPrincipal)
        .where(PlatformPrincipal.principal_id == principal.principal_id)
        .values(permissions=[PLATFORM_AI_READ, PLATFORM_AI_WRITE])
    )
    await db_session.refresh(principal)
    assert principal.row_version == 4

    await db_session.execute(
        update(PlatformPrincipal)
        .where(PlatformPrincipal.principal_id == principal.principal_id)
        .values(hashed_password=get_password_hash("rotated-password2"))
    )
    await db_session.refresh(principal)
    assert principal.row_version == 5

    with pytest.raises(BusinessException) as exc_info:
        await authenticate_platform_token(old_token, db_session)
    assert exc_info.value.error_code == "PLATFORM_TOKEN_INVALID"

    await db_session.execute(
        update(PlatformPrincipal)
        .where(PlatformPrincipal.principal_id == principal.principal_id)
        .values(last_login_at=datetime.now(UTC))
    )
    await db_session.refresh(principal)
    assert principal.row_version == 5


async def test_platform_audit_rows_reject_update_and_delete(db_session):
    principal = PlatformPrincipal(
        principal_name="test_platform_auditor",
        display_name="Test Platform Auditor",
        hashed_password=get_password_hash("a-long-test-password"),
        permissions=[PLATFORM_AI_READ],
    )
    db_session.add(principal)
    await db_session.flush()
    event = PlatformAuditLog(
        actor_principal_id=principal.principal_id,
        actor_name=principal.principal_name,
        permission=PLATFORM_AI_READ,
        event_type="authorized",
        method="GET",
        path="/ai/provider/list",
        reason="Verify append-only storage",
        ticket_id="TEST-APPEND-ONLY",
        correlation_id="test-append-only",
    )
    db_session.add(event)
    await db_session.flush()

    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            await db_session.execute(
                insert(PlatformAuditLog).values(
                    audit_id=event.audit_id + 1,
                    authorization_audit_id=event.audit_id,
                    actor_principal_id=principal.principal_id,
                    actor_name=principal.principal_name,
                    permission="platform:ai:write",
                    event_type="completed",
                    method="GET",
                    path="/ai/provider/list",
                    reason=event.reason,
                    ticket_id=event.ticket_id,
                    correlation_id=event.correlation_id,
                    status_code=200,
                    duration_ms=0,
                )
            )

    completion = PlatformAuditLog(
        authorization_audit_id=event.audit_id,
        actor_principal_id=principal.principal_id,
        actor_name=principal.principal_name,
        permission=event.permission,
        event_type="completed",
        method=event.method,
        path=event.path,
        reason=event.reason,
        ticket_id=event.ticket_id,
        correlation_id=event.correlation_id,
        status_code=200,
        duration_ms=0,
    )
    db_session.add(completion)
    await db_session.flush()

    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            await db_session.execute(
                insert(PlatformAuditLog).values(
                    audit_id=completion.audit_id + 1,
                    authorization_audit_id=event.audit_id,
                    actor_principal_id=principal.principal_id,
                    actor_name=principal.principal_name,
                    permission=event.permission,
                    event_type="completed",
                    method=event.method,
                    path=event.path,
                    reason=event.reason,
                    ticket_id=event.ticket_id,
                    correlation_id=event.correlation_id,
                    status_code=200,
                    duration_ms=0,
                )
            )

    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            await db_session.execute(
                update(PlatformAuditLog)
                .where(PlatformAuditLog.audit_id == event.audit_id)
                .values(denial_code="tampered")
            )

    with pytest.raises(DBAPIError):
        async with db_session.begin_nested():
            await db_session.execute(
                delete(PlatformAuditLog).where(
                    PlatformAuditLog.audit_id == event.audit_id
                )
            )


async def test_platform_completion_replay_is_idempotent_but_status_conflict_fails(
    db_session,
):
    principal = PlatformPrincipal(
        principal_name="test_platform_completion",
        display_name="Test Platform Completion",
        hashed_password=get_password_hash("a-long-test-password1"),
        permissions=[PLATFORM_AI_READ],
    )
    db_session.add(principal)
    await db_session.flush()
    event = PlatformAuditLog(
        actor_principal_id=principal.principal_id,
        actor_name=principal.principal_name,
        permission=PLATFORM_AI_READ,
        event_type="authorized",
        method="GET",
        path="/ai/provider/list",
        reason="Verify completion idempotency",
        ticket_id="TEST-COMPLETION",
        correlation_id="test-completion",
    )
    db_session.add(event)
    await db_session.flush()
    values = {
        "authorization_audit_id": event.audit_id,
        "actor_principal_id": principal.principal_id,
        "actor_name": principal.principal_name,
        "permission": event.permission,
        "method": event.method,
        "path": event.path,
        "reason": event.reason,
        "ticket_id": event.ticket_id,
        "correlation_id": event.correlation_id,
        "ip": "127.0.0.1",
        "target_tenant_id": None,
        "status_code": 200,
        "duration_ms": 7,
        "result_summary": {"statusCode": 200},
    }

    first_id = await add_platform_completion(db_session, **values)
    replay_id = await add_platform_completion(db_session, **values)

    assert replay_id == first_id
    with pytest.raises(BusinessException) as exc_info:
        await add_platform_completion(
            db_session,
            **(values | {"status_code": 500, "result_summary": {"statusCode": 500}}),
        )
    assert exc_info.value.error_code == "PLATFORM_AUDIT_COMPLETION_CONFLICT"
