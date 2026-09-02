"""Release containment for Marketplace and Lowcode tenant capabilities."""

from fastapi import Depends

from app.core.config import settings
from app.core.exceptions import AuthorizationException
from app.core.tenant import DEFAULT_TENANT_ID, TenantContext
from app.modules.auth.service import get_current_tenant_context

MARKETPLACE_HOSTED_UNAVAILABLE = "MARKETPLACE_HOSTED_UNAVAILABLE"


def require_marketplace_capability(tenant: TenantContext) -> None:
    """Allow the legacy capability only for Default Tenant in single mode.

    This check intentionally precedes every DB, Redis, file, DDL, or provider
    operation.  Marketplace/Lowcode is contained—not multi-tenant capable—in
    the first hosted release.
    """
    if settings.TENANT_MODE != "single" or tenant.tenant_id != DEFAULT_TENANT_ID:
        raise AuthorizationException(
            "当前部署未开放应用市场与低代码能力",
            error_code=MARKETPLACE_HOSTED_UNAVAILABLE,
        )


def require_marketplace_http_capability(
    tenant: TenantContext = Depends(get_current_tenant_context),
) -> None:
    """FastAPI dependency for handler-before-side-effect containment."""
    require_marketplace_capability(tenant)
