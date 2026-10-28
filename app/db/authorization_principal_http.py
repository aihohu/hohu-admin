"""Fixed Principal support/retention routes; no ordinary request transaction."""

from dataclasses import dataclass
from types import MappingProxyType, SimpleNamespace

from anyio import CancelScope
from fastapi import Depends, Request
from fastapi.exceptions import RequestValidationError
from fastapi.security import HTTPAuthorizationCredentials

from app.core.exceptions import AuthenticationException
from app.db.authorization_control_http import _identifier
from app.db.authorization_http import get_authorization_runtime
from app.db.authorization_plan_session import _order_error
from app.modules.auth.service import platform_bearer_scheme
from app.modules.platform.audit import (
    authorize_platform_request,
    decode_platform_reason,
)

PRINCIPAL_HTTP = MappingProxyType(
    {
        ("GET", "/platform/tenants/{tenant_id}/support/operation-logs"): (
            "support.operation_logs",
            "platform:support:read",
        ),
        ("GET", "/platform/tenants/{tenant_id}/support/login-logs"): (
            "support.login_logs",
            "platform:support:read",
        ),
        ("POST", "/platform/tenants/{tenant_id}/audit-retention/preview"): (
            "retention.preview",
            "platform:audit:retention",
        ),
        ("POST", "/platform/tenants/{tenant_id}/audit-retention/purge"): (
            "retention.purge",
            "platform:audit:retention",
        ),
    }
)


@dataclass(slots=True)
class PrincipalHttp:
    runtime: object
    identity: object
    audit: object
    operation: str
    target_tenant_id: int
    attempted: bool = False

    async def execute(self, *, target_tenant_id, **request):
        if (
            self.attempted
            or type(target_tenant_id) is not int
            or target_tenant_id != self.target_tenant_id
        ):
            raise _order_error()
        self.attempted = True
        return await self.runtime.principal_control(
            identity=self.identity,
            operation=self.operation,
            target_tenant_id=target_tenant_id,
            issued_audit=self.audit,
            **request,
        )


async def require_principal_control(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(platform_bearer_scheme),
):
    from app.modules.platform.authorization_principal_audit import (  # noqa: PLC0415
        principal_audit_consumed,
    )

    if credentials is None:
        raise AuthenticationException(
            "缺少平台 Token", error_code="PLATFORM_TOKEN_REQUIRED"
        )
    path = request.scope["route"].path
    entry = PRINCIPAL_HTTP.get((request.method, path))
    if entry is None:
        raise _order_error()
    operation, permission = entry
    target = _identifier(request, "tenant_id", zero=True)
    runtime = get_authorization_runtime()
    principal = await runtime.principal_current(token=credentials.credentials)
    audit = None

    async def persist(**values):
        nonlocal audit
        audit = await runtime.authorize_principal(
            identity=principal.identity,
            permission=permission,
            target_tenant_id=target,
            path=path,
            method=request.method,
            operation=operation,
            **{
                key: values.get(key)
                for key in (
                    "reason",
                    "ticket_id",
                    "correlation_id",
                    "event_type",
                    "denial_code",
                    "status_code",
                )
            },
        )
        return audit.authorization_audit_id

    await authorize_platform_request(
        principal=SimpleNamespace(
            principal_id=principal.identity.principal_id,
            principal_name=principal.principal_name,
            permissions=frozenset(principal.permissions),
        ),
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
        target_tenant_id=target,
        persist=persist,
    )
    try:
        yield PrincipalHttp(runtime, principal.identity, audit, operation, target)
    except BaseException as error:
        if not principal_audit_consumed(audit):
            with CancelScope(shield=True):
                await runtime.fail_principal(
                    audit,
                    SimpleNamespace(code=422)
                    if isinstance(error, RequestValidationError)
                    else error,
                )
        raise
