"""Fixed setting groups, separate from custom parameter CRUD."""

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import is_super_admin, require_permissions
from app.core.base_response import ResponseModel
from app.core.cache import cache_delete
from app.core.exceptions import AuthorizationException
from app.core.tenant import TenantContext, TenantLocatorContext
from app.db.session import get_db
from app.modules.auth.service import (
    get_current_tenant_context,
    get_public_tenant_context,
)
from app.modules.system.models.user import User
from app.modules.system.service.file_policy_service import file_policy_service
from app.modules.system.service.settings_service import settings_service
from app.modules.system.settings_catalog import SETTING_GROUPS

router = APIRouter()


class SettingsWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")
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
        "secretKeys": group["secret_keys"],
        "configuredSecrets": group["configured_secrets"],
    }


@router.get("/public")
async def public_settings(
    db: AsyncSession = Depends(get_db),
    tenant: TenantLocatorContext = Depends(get_public_tenant_context),
):
    return ResponseModel.success(
        data=await settings_service.get_public_values(db, tenant=tenant)
    )


@router.get("/runtime")
async def runtime_settings(
    db: AsyncSession = Depends(get_db),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    values = await settings_service.values(db, tenant_id=tenant.tenant_id)
    return ResponseModel.success(
        data={
            "defaultLocale": values["default_locale"],
            "requirePrimaryDept": values["user_require_primary_dept"],
            "brand": {
                key: value for key, value in values.items() if key.startswith("site_")
            },
            "defaultAvatar": values["default_avatar"],
            "uploads": await file_policy_service.describe(db, tenant=tenant),
        }
    )


@router.get("")
async def groups(
    user: User = Depends(require_permissions("system:setting:list")),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    return ResponseModel.success(
        data=[
            g
            for g in SETTING_GROUPS
            if g != "security" or (tenant.tenant_id == 0 and is_super_admin(user))
        ]
    )


@router.get("/{group}")
async def get_group(
    group: str,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permissions("system:setting:list")),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    _ensure_group_access(group, user, tenant)
    return ResponseModel.success(
        data=_response(await settings_service.get_group(db, group, tenant=tenant))
    )


@router.put("/{group}")
async def save_group(
    group: str,
    payload: SettingsWrite,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permissions("system:setting:edit")),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    _ensure_group_access(group, user, tenant)
    result = await settings_service.save_group(
        db, group, payload.values, revision=payload.revision, tenant=tenant
    )
    await db.commit()
    await cache_delete(pattern=f"tenant:{tenant.tenant_id}:setting:*")
    return ResponseModel.success(data=_response(result))
