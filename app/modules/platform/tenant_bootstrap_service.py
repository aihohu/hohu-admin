"""Platform coordinator for one-shot prepared tenant bootstrap."""

import hashlib
import hmac
import json
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.exceptions import BusinessRuleException
from app.core.tenant import PlatformContext, require_platform_permission
from app.modules.ai.service.tenant_bootstrap_service import (
    ai_tenant_bootstrap_service,
)
from app.modules.platform.constants import PLATFORM_TENANT_BOOTSTRAP
from app.modules.system.service.tenant_bootstrap_service import (
    system_tenant_bootstrap_service,
)
from app.modules.system.service.tenant_lifecycle_service import (
    hash_tenant_idempotency_key,
)
from app.utils.validators import PWD_ERROR_CODE, PWD_ERROR_MSG, validate_password


@dataclass(frozen=True, slots=True)
class TenantBootstrapResult:
    tenant_code: str
    lifecycle_state: str
    admin_username: str
    model_label: str
    menu_count: int
    role_count: int
    model_policy_count: int
    agent_binding_count: int
    replayed: bool


def _bootstrap_fingerprint(
    *, tenant_id: int, default_model_id: int, admin_password: str
) -> str:
    payload = json.dumps(
        {
            "adminPassword": admin_password,
            "bootstrapVersion": 1,
            "defaultModelId": str(default_model_id),
            "tenantId": str(tenant_id),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hmac.new(settings.SECRET_KEY.encode(), payload, hashlib.sha256).hexdigest()


class TenantBootstrapService:
    async def bootstrap(
        self,
        db: AsyncSession,
        *,
        tenant_id: int,
        default_model_id: int,
        admin_password: str,
        idempotency_key: str,
        platform: PlatformContext,
    ) -> TenantBootstrapResult:
        require_platform_permission(platform, PLATFORM_TENANT_BOOTSTRAP)
        if (
            isinstance(default_model_id, bool)
            or not isinstance(default_model_id, int)
            or default_model_id <= 0
            or default_model_id > 9_223_372_036_854_775_807
        ):
            raise BusinessRuleException(
                "defaultModelId 无效",
                error_code="PLATFORM_TENANT_BOOTSTRAP_MODEL_ID_INVALID",
            )
        try:
            validate_password(admin_password)
        except (TypeError, ValueError) as exc:
            raise BusinessRuleException(
                PWD_ERROR_MSG,
                error_code=PWD_ERROR_CODE,
            ) from exc
        key_hash = hash_tenant_idempotency_key(idempotency_key)
        fingerprint = _bootstrap_fingerprint(
            tenant_id=tenant_id,
            default_model_id=default_model_id,
            admin_password=admin_password,
        )
        async with db.begin_nested():
            tenant, replayed = await system_tenant_bootstrap_service.lock_target(
                db,
                tenant_id=tenant_id,
                key_hash=key_hash,
                fingerprint=fingerprint,
                platform=platform,
            )
            if replayed:
                system_summary = await system_tenant_bootstrap_service.summarize(
                    db, tenant_id=tenant_id, platform=platform
                )
                ai_summary = await ai_tenant_bootstrap_service.summarize(
                    db, tenant_id=tenant_id, platform=platform
                )
            else:
                model, provider = await ai_tenant_bootstrap_service.validate_model(
                    db,
                    model_id=default_model_id,
                    tenant_id=tenant_id,
                    platform=platform,
                )
                system_summary = await system_tenant_bootstrap_service.seed(
                    db,
                    tenant=tenant,
                    admin_password=admin_password,
                    platform=platform,
                )
                if system_summary.super_role_id is None:
                    raise RuntimeError("tenant bootstrap super role was not created")
                ai_summary = await ai_tenant_bootstrap_service.seed(
                    db,
                    tenant_id=tenant_id,
                    super_role_id=system_summary.super_role_id,
                    model=model,
                    provider=provider,
                    platform=platform,
                )
                await system_tenant_bootstrap_service.complete(
                    db,
                    tenant=tenant,
                    key_hash=key_hash,
                    fingerprint=fingerprint,
                    platform=platform,
                )
        return TenantBootstrapResult(
            tenant_code=tenant.tenant_code,
            lifecycle_state=tenant.lifecycle_state,
            admin_username=system_summary.admin_username,
            model_label=ai_summary.model_label,
            menu_count=system_summary.menu_count,
            role_count=system_summary.role_count,
            model_policy_count=ai_summary.model_policy_count,
            agent_binding_count=ai_summary.agent_binding_count,
            replayed=replayed,
        )


tenant_bootstrap_service = TenantBootstrapService()
