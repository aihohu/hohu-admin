"""Public account capabilities, registration/recovery and authenticated email binding."""

import logging

from fastapi import APIRouter, Depends, Query, Request

from app.core.base_response import ResponseModel
from app.core.config import settings
from app.core.exceptions import BusinessRuleException
from app.core.tenant import TenantLocatorContext
from app.db.authorization_http import (
    get_authorization_runtime,
    release_request_reads,
    user_read,
)
from app.db.session import get_db
from app.modules.auth.schemas.auth import LoginCredentials
from app.modules.auth.schemas.self_service import (
    EmailBinding,
    EmailCodeRequest,
    Registration,
)
from app.modules.auth.service import (
    access_identity,
    auth_service,
    get_current_user,
    oauth2_scheme,
)
from app.modules.system.service.mail_service import mail_service, ready
from app.modules.system.service.settings_service import settings_service
from app.utils.ip_util import get_client_ip

router = APIRouter()
logger = logging.getLogger(__name__)


async def resolve_tenant(request, db, code):
    host = request.headers.get("Host")
    if (
        settings.TENANT_MODE == "hosted"
        and not code
        and not auth_service._host_tenant_code(host)
    ):
        raise BusinessRuleException(
            "Select a tenant first", error_code="TENANT_REQUIRED"
        )
    return await auth_service.resolve_login_tenant(
        LoginCredentials(tenant_code=code), db, host=host
    )


async def policy(db, tenant_id, purpose):
    values = await settings_service.values(db, tenant_id=tenant_id)
    config = await mail_service.config(db)
    key = "register_enabled" if purpose == "register" else "password_reset_enabled"
    if not values[key]:
        raise BusinessRuleException(
            "This account service is disabled", error_code="ACCOUNT_SERVICE_DISABLED"
        )
    if not ready(config):
        raise BusinessRuleException(
            "Mail is unavailable", error_code="MAIL_NOT_CONFIGURED"
        )
    return values, config


@router.get("/capabilities")
async def capabilities(
    request: Request,
    tenant_code: str | None = Query(None, alias="tenantCode"),
    db=Depends(get_db),
):
    tenant = await resolve_tenant(request, db, tenant_code)
    locator = TenantLocatorContext(
        tenant.tenant_id, tenant.tenant_code, tenant.row_version
    )
    await release_request_reads(db)
    return ResponseModel.success(
        data=await get_authorization_runtime().parameter_read(
            tenant=locator, actor_user_version=None, operation="auth.capabilities"
        )
    )


@router.post("/email-code")
async def send_code(
    payload: EmailCodeRequest,
    request: Request,
    db=Depends(get_db),
):
    tenant = await resolve_tenant(request, db, payload.tenant_code)
    tenant_id = tenant.tenant_id
    await release_request_reads(db)
    data = await get_authorization_runtime().prepare_email(
        tenant_id, payload, ip=get_client_ip(request)
    )
    return ResponseModel.success(data=data)


@router.post("/register")
async def register(payload: Registration, request: Request, db=Depends(get_db)):
    tenant = await resolve_tenant(request, db, payload.tenant_code)
    tenant_id = tenant.tenant_id
    await release_request_reads(db)
    user_id = await get_authorization_runtime().register(tenant_id, payload)
    return ResponseModel.success(data={"userId": str(user_id)})


@router.post("/password-reset")
async def reset_password(payload: Registration, request: Request, db=Depends(get_db)):
    tenant = await resolve_tenant(request, db, payload.tenant_code)
    tenant_id = tenant.tenant_id
    await release_request_reads(db)
    await get_authorization_runtime().reset_password(tenant_id, payload)
    return ResponseModel.success()


@router.get("/email")
async def email_status(user=Depends(get_current_user), db=Depends(get_db)):
    return ResponseModel.success(data=await user_read(db, user, operation="email"))


@router.post("/email/code")
async def binding_code(
    payload: EmailBinding,
    request: Request,
    user=Depends(get_current_user),
    db=Depends(get_db),
    token: str = Depends(oauth2_scheme),
):
    identity = access_identity(user)
    await release_request_reads(db)
    data = await get_authorization_runtime().prepare_email(
        identity.tenant.tenant_id, payload, ip=get_client_ip(request), token=token
    )
    return ResponseModel.success(data=data)


@router.put("/email")
async def bind_email(
    payload: EmailBinding,
    user=Depends(get_current_user),
    db=Depends(get_db),
    token: str = Depends(oauth2_scheme),
):
    access_identity(user)
    await release_request_reads(db)
    await get_authorization_runtime().bind_email(token, payload)
    return ResponseModel.success()
