"""Fixed setting groups, separate from custom parameter CRUD."""

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, EmailStr, Field
from pydantic.alias_generators import to_camel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, is_super_admin, require_permissions
from app.core.base_response import ResponseModel
from app.core.exceptions import AuthorizationException
from app.core.rbac import is_system_admin
from app.core.tenant import TenantContext, TenantLocatorContext
from app.db.authorization_http import (
    control_write,
    get_authorization_runtime,
    release_request_reads,
)
from app.db.session import get_db
from app.modules.auth.service import (
    access_identity,
    get_current_tenant_context,
    get_public_tenant_context,
)
from app.modules.system.models.user import User
from app.modules.system.service.mail_service import MailWrite

router = APIRouter()


def _mail_admin(user):
    if not is_system_admin(user):
        raise AuthorizationException(
            "System administrator required", error_code="SUPER_ADMIN_ONLY"
        )


class MailTest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient: EmailStr


@router.get("/mail")
async def read_mail(
    db=Depends(get_db), user=Depends(require_permissions("system:setting:list"))
):
    _mail_admin(user)
    identity = access_identity(user)
    await release_request_reads(db)
    return ResponseModel.success(
        data=await get_authorization_runtime().parameter_read(
            tenant=identity.tenant,
            actor_user_version=identity.user_version,
            operation="mail.describe",
        )
    )


@router.put("/mail")
async def write_mail(
    payload: MailWrite,
    request: Request,
    db=Depends(get_db),
    user=Depends(require_permissions("system:setting:edit")),
):
    _mail_admin(user)
    result = await control_write(
        db, user, operation="mail.save", payload=payload, request=request
    )
    return ResponseModel.success(data=result)


@router.post("/mail/test")
async def test_mail(
    payload: MailTest,
    request: Request,
    db=Depends(get_db),
    user=Depends(require_permissions("system:setting:edit")),
):
    _mail_admin(user)
    result = await control_write(
        db, user, operation="mail.test", payload=payload.model_dump(), request=request
    )
    return ResponseModel.success(data=result)


class SettingsWrite(BaseModel):
    model_config = ConfigDict(
        extra="forbid", alias_generator=to_camel, populate_by_name=True
    )
    form_receipt: str | None = Field(None, max_length=2048, repr=False)
    values: dict[str, object]
    revision: str = Field(min_length=64, max_length=64)


def _ensure_group_access(group: str, user: User, tenant: TenantContext) -> None:
    if group == "security" and (tenant.tenant_id != 0 or not is_super_admin(user)):
        raise AuthorizationException(
            "System administrator required", error_code="SUPER_ADMIN_ONLY"
        )


def _response(group: dict) -> dict:
    return {
        **{
            k: v
            for k, v in group.items()
            if k not in {"secret_keys", "configured_secrets"}
        },
        "secretKeys": group.get("secretKeys", group.get("secret_keys", [])),
        "configuredSecrets": group.get(
            "configuredSecrets", group.get("configured_secrets", [])
        ),
    }


@router.get("/public")
async def public_settings(
    db: AsyncSession = Depends(get_db),
    tenant: TenantLocatorContext = Depends(get_public_tenant_context),
):
    await release_request_reads(db)
    return ResponseModel.success(
        data=await get_authorization_runtime().parameter_read(
            tenant=tenant, actor_user_version=None, operation="settings.public"
        )
    )


@router.get("/runtime")
async def runtime_settings(
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_current_tenant_context),
    user: User = Depends(get_current_user),
):
    identity = access_identity(user)
    await release_request_reads(db)
    return ResponseModel.success(
        data=await get_authorization_runtime().parameter_read(
            tenant=tenant,
            actor_user_version=identity.user_version,
            operation="settings.runtime",
        )
    )


@router.get("")
async def groups(
    user: User = Depends(require_permissions("system:setting:list")),
    tenant: TenantContext = Depends(get_current_tenant_context),
    db: AsyncSession = Depends(get_db),
):
    identity = access_identity(user)
    await release_request_reads(db)
    return ResponseModel.success(
        data=await get_authorization_runtime().parameter_read(
            tenant=tenant,
            actor_user_version=identity.user_version,
            operation="settings.groups",
        )
    )


@router.get("/{group}")
async def get_group(
    group: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permissions("system:setting:list")),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    _ensure_group_access(group, user, tenant)
    identity = access_identity(user)
    await release_request_reads(db)
    return ResponseModel.success(
        data=await get_authorization_runtime().parameter_read(
            tenant=tenant,
            actor_user_version=identity.user_version,
            operation="settings.group",
            query=group,
        )
    )


@router.put("/{group}")
async def save_group(
    group: str,
    payload: SettingsWrite,
    request: Request,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permissions("system:setting:edit")),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    _ensure_group_access(group, user, tenant)
    result = await control_write(
        db,
        user,
        operation="settings.save",
        payload={
            "group": group,
            "values": payload.values,
            "revision": payload.revision,
        },
        tenant=tenant,
        request=request,
        protocol_receipt=payload.form_receipt,
    )
    return ResponseModel.success(data=_response(result))
