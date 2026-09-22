"""Agent-only system role authorization using the ordinary login session."""

import json
import time
from collections.abc import AsyncGenerator
from types import SimpleNamespace

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import AuthorizationException, BusinessException
from app.core.rbac import is_system_admin
from app.core.tenant import PlatformContext
from app.db.session import AsyncSessionLocal, get_db
from app.modules.auth.service import get_current_user
from app.modules.platform.audit import (
    authorize_platform_request,
    decode_platform_reason,
)
from app.modules.platform.constants import (
    PLATFORM_AI_READ,
    PLATFORM_AI_WRITE,
    platform_permission_for_request,
)
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.models.user import User


def system_agent_audit_record(
    *, user_id: int, tenant_id: int, **values
) -> SysOperationLog:
    """Map the human actor to user audit, never a platform-principal foreign key."""
    return SysOperationLog(
        tenant_id=tenant_id,
        audit_scope="platform",
        user_id=user_id,
        username=values["actor_name"],
        module="Agent管理",
        action=values["event_type"],
        method=values["method"],
        path=values["path"],
        status_code=values.get("status_code"),
        ip=values.get("ip"),
        duration=values.get("duration_ms"),
        request_params=json.dumps(
            {
                key: values.get(key)
                for key in (
                    "permission",
                    "reason",
                    "ticket_id",
                    "correlation_id",
                    "denial_code",
                    "authorization_audit_id",
                    "changes",
                )
            },
            ensure_ascii=False,
        ),
    )


async def persist_system_agent_audit(**values) -> int:
    # HTTP authorization intent survives a later business rollback.
    async with AsyncSessionLocal() as session:
        record = system_agent_audit_record(**values)
        session.add(record)
        await session.commit()
        return record.operation_log_id


async def require_system_agent_context(
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> AsyncGenerator[PlatformContext]:
    if not is_system_admin(user):
        raise AuthorizationException(
            "仅系统超级管理员可管理全局 Agent", error_code="SYSTEM_ADMIN_ONLY"
        )
    started = time.perf_counter()
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    permission = platform_permission_for_request(request.method, path)
    principal = SimpleNamespace(
        principal_id=user.user_id,
        principal_name=user.user_name,
        permissions=frozenset({PLATFORM_AI_READ, PLATFORM_AI_WRITE}),
    )

    async def persist(**values):
        return await persist_system_agent_audit(
            user_id=user.user_id, tenant_id=user.tenant_id, **values
        )

    authorization = await authorize_platform_request(
        principal=principal,
        permission=permission,
        method=request.method,
        path=path,
        reason=decode_platform_reason(
            request.headers.get("X-Platform-Reason"),
            request.headers.get("X-Platform-Reason-Encoding"),
        ),
        ticket_id=request.headers.get("X-Platform-Ticket"),
        correlation_id=request.headers.get("X-Correlation-ID"),
        ip=request.client.host if request.client else None,
        request_summary={"queryKeyCount": len(request.query_params)},
        persist=persist,
    )
    context = authorization.context
    completion = {
        "user_id": user.user_id,
        "tenant_id": user.tenant_id,
        "actor_name": user.user_name,
        "permission": permission,
        "method": request.method,
        "path": path,
        "reason": context.reason,
        "ticket_id": context.ticket_id,
        "correlation_id": context.correlation_id,
        "authorization_audit_id": authorization.authorization_audit_id,
        "event_type": "completed",
    }
    try:
        yield context
    except Exception as exc:
        await persist_system_agent_audit(
            **completion,
            status_code=getattr(exc, "code", 500),
            duration_ms=int((time.perf_counter() - started) * 1000),
        )
        raise
    else:
        # get_db commits this completion atomically with the Agent update.
        try:
            db.add(
                system_agent_audit_record(
                    **completion,
                    changes=getattr(request.state, "system_agent_changes", None),
                    status_code=200,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )
            )
            await db.flush()
        except Exception as exc:
            raise BusinessException(
                code=503,
                message="系统管理审计暂不可用",
                error_code="PLATFORM_AUDIT_UNAVAILABLE",
            ) from exc
