"""Fixed platform write routes with one issued intent and one completion owner."""

from dataclasses import dataclass
from types import MappingProxyType, SimpleNamespace

from anyio import CancelScope
from fastapi import Depends, Request
from fastapi.exceptions import RequestValidationError

from app.core.exceptions import AuthorizationException
from app.core.rbac import is_system_admin
from app.db.authorization_http import get_authorization_runtime, release_request_reads
from app.db.authorization_plan_session import _order_error
from app.db.session import get_db
from app.modules.auth.service import access_identity, get_current_user
from app.modules.platform.audit import (
    authorize_platform_request,
    decode_platform_reason,
)
from app.modules.platform.constants import PLATFORM_AI_WRITE
from app.modules.system.service.authorization_control_audit import (
    control_audit_consumed,
)

_P = "/platform/ai/providers"
_M = "/platform/tenants/{tenant_id}/ai/model-policies"
CATALOG_HTTP = MappingProxyType(
    {
        ("PUT", "/platform/ai/agents/{agent_id}"): ("agent.update", "agent_id", None),
        ("POST", _P): ("provider.create", None, None),
        ("PUT", _P + "/{provider_id}"): ("provider.update", "provider_id", None),
        ("DELETE", _P + "/{provider_id}"): ("provider.delete", "provider_id", None),
        ("POST", _P + "/{provider_id}/models"): ("model.create", None, "provider_id"),
        ("PUT", _P + "/{provider_id}/models/{model_id}"): (
            "model.update",
            "model_id",
            "provider_id",
        ),
        ("DELETE", _P + "/{provider_id}/models/{model_id}"): (
            "model.delete",
            "model_id",
            "provider_id",
        ),
        ("POST", _P + "/test"): ("provider.probe_draft", None, None),
        ("POST", _P + "/{provider_id}/test"): ("provider.probe", None, "provider_id"),
        ("PUT", _M): ("model_policy.put_many", None, None),
        ("PUT", _M + "/{model_id}"): ("model_policy.put", "model_id", None),
        ("DELETE", _M + "/{model_id}"): ("model_policy.delete", "model_id", None),
    }
)
TENANT_HTTP = MappingProxyType(
    {
        ("POST", "/platform/tenants"): ("tenant.prepare", None, None),
        ("POST", "/platform/tenants/{tenant_id}/activate"): (
            "tenant.activate",
            None,
            None,
        ),
        ("POST", "/platform/tenants/{tenant_id}/disable"): (
            "tenant.disable",
            None,
            None,
        ),
        ("POST", "/platform/tenants/{tenant_id}/bootstrap"): (
            "tenant.bootstrap",
            None,
            None,
        ),
    }
)
CONTROL_HTTP = MappingProxyType(dict(CATALOG_HTTP) | dict(TENANT_HTTP))
_TENANT_PERMISSIONS = {
    "tenant.prepare": "platform:tenant:write",
    "tenant.disable": "platform:tenant:write",
    "tenant.activate": "platform:tenant:activate",
    "tenant.bootstrap": "platform:tenant:bootstrap",
}


def _identifier(request, name, *, zero=False):
    if name is None:
        return None
    value = request.path_params.get(name)
    if (
        type(value) is str
        and 1 <= len(value) <= 19
        and value.isascii()
        and value.isdecimal()
    ):
        value = int(value)
    if type(value) is not int or not (0 if zero else 1) <= value < 2**63:
        raise RequestValidationError(
            [
                {
                    "type": "value_error",
                    "loc": ("path", name),
                    "msg": "Invalid path identifier",
                    "input": None,
                }
            ]
        )
    return value


@dataclass(slots=True)
class ControlHttp:
    runtime: object
    identity: object
    audit: object
    operation: str
    target_id: int | None
    provider_id: int | None
    target_tenant_id: int | None
    attempted: bool = False

    async def execute(
        self,
        payload=None,
        *,
        model_id=None,
        target_id=None,
        provider_id=None,
        target_tenant_id=None,
    ):
        if self.attempted:
            raise _order_error()
        for actual, expected in (
            (target_id, self.target_id),
            (provider_id, self.provider_id),
            (target_tenant_id, self.target_tenant_id),
        ):
            if actual is not None and (type(actual) is not int or actual != expected):
                raise _order_error()
        if model_id is not None and self.operation != "provider.probe":
            raise _order_error()
        self.attempted = True
        return await self.runtime.control(
            tenant=self.identity.tenant,
            actor_user_version=self.identity.user_version,
            operation=self.operation,
            target_id=model_id
            if self.operation == "provider.probe"
            else self.target_id,
            provider_id=self.provider_id,
            target_tenant_id=self.target_tenant_id,
            payload=payload,
            issued_audit=self.audit,
            http_result=True,
        )


async def require_control_write(
    request: Request, user=Depends(get_current_user), db=Depends(get_db)
):
    if not is_system_admin(user):
        raise AuthorizationException(
            "仅系统超级管理员可执行全局管理", error_code="SYSTEM_ADMIN_ONLY"
        )
    path = request.scope["route"].path
    entry = CONTROL_HTTP.get((request.method, path))
    if entry is None:
        raise _order_error()
    operation, target_name, provider_name = entry
    permission = _TENANT_PERMISSIONS.get(operation, PLATFORM_AI_WRITE)
    identity = access_identity(user)
    target = _identifier(request, target_name)
    provider = _identifier(request, provider_name)
    tenant = (
        _identifier(request, "tenant_id", zero=True) if "{tenant_id}" in path else None
    )
    principal = SimpleNamespace(
        principal_id=user.user_id,
        principal_name=user.user_name,
        permissions=frozenset({permission}),
    )
    await release_request_reads(db)
    runtime = get_authorization_runtime()
    audit = None

    async def persist(**values):
        nonlocal audit
        result = await runtime.authorize_control(
            tenant=identity.tenant,
            actor_user_version=identity.user_version,
            operation=operation,
            permission=permission,
            target_tenant_id=tenant,
            path=path,
            method=request.method,
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
        if values["event_type"] == "authorized":
            audit = result
            return result.authorization_audit_id
        return result.completion_id

    await authorize_platform_request(
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
        target_tenant_id=tenant,
        persist=persist,
    )
    context = ControlHttp(runtime, identity, audit, operation, target, provider, tenant)
    try:
        yield context
    except BaseException as error:
        if not control_audit_consumed(audit):
            # Validation can fail after dependencies have persisted the intent.
            failure = (
                SimpleNamespace(code=422)
                if isinstance(error, RequestValidationError)
                else error
            )
            with CancelScope(shield=True):
                await runtime.fail_control(audit, failure)
        raise
