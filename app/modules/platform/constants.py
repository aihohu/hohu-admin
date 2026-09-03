"""Stable platform permission codes and hosted control-plane route mapping."""

import re

from app.core.exceptions import AuthorizationException

PLATFORM_AI_READ = "platform:ai:read"
PLATFORM_AI_WRITE = "platform:ai:write"
PLATFORM_TENANT_READ = "platform:tenant:read"
PLATFORM_TENANT_WRITE = "platform:tenant:write"
PLATFORM_TENANT_BOOTSTRAP = "platform:tenant:bootstrap"
PLATFORM_SUPPORT_READ = "platform:support:read"
PLATFORM_AUDIT_RETENTION = "platform:audit:retention"
# Offline maintenance action recorded in platform audit. It is deliberately not
# assignable to an online token; local DB access plus current-password proof is
# the authorization boundary for this recovery-safe permission replacement.
PLATFORM_PRINCIPAL_PERMISSION_REPLACE = "platform:principal:permissions:replace"

ASSIGNABLE_PLATFORM_PERMISSIONS = frozenset(
    {
        PLATFORM_AI_READ,
        PLATFORM_AI_WRITE,
        PLATFORM_TENANT_READ,
        PLATFORM_TENANT_WRITE,
        PLATFORM_TENANT_BOOTSTRAP,
        PLATFORM_SUPPORT_READ,
        PLATFORM_AUDIT_RETENTION,
    }
)
PLATFORM_PRINCIPAL_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{2,63}$")


def platform_permission_for_request(method: str, path: str) -> str:
    """Map only explicitly governed compatibility paths; unknown paths fail closed."""
    normalized_method = method.upper()
    if path == "/platform/tenants":
        if normalized_method in {"GET", "HEAD"}:
            return PLATFORM_TENANT_READ
        if normalized_method == "POST":
            return PLATFORM_TENANT_WRITE
    if path == "/platform/tenants/{tenant_id}":
        if normalized_method in {"GET", "HEAD"}:
            return PLATFORM_TENANT_READ
    if path == "/platform/tenants/{tenant_id}/disable":
        if normalized_method == "POST":
            return PLATFORM_TENANT_WRITE
    if path == "/platform/tenants/{tenant_id}/bootstrap":
        if normalized_method == "POST":
            return PLATFORM_TENANT_BOOTSTRAP
    if path in {
        "/platform/tenants/{tenant_id}/support/operation-logs",
        "/platform/tenants/{tenant_id}/support/login-logs",
    }:
        if normalized_method in {"GET", "HEAD"}:
            return PLATFORM_SUPPORT_READ
    if path in {
        "/platform/tenants/{tenant_id}/audit-retention/preview",
        "/platform/tenants/{tenant_id}/audit-retention/purge",
    }:
        if normalized_method == "POST":
            return PLATFORM_AUDIT_RETENTION

    governed = path == "/ai/admin/agents" or path.startswith("/ai/admin/agents/")
    governed = governed or path == "/ai/provider" or path.startswith("/ai/provider/")
    if not governed:
        raise AuthorizationException(
            "平台入口尚未映射权限",
            error_code="PLATFORM_PERMISSION_UNMAPPED",
        )
    return (
        PLATFORM_AI_READ if normalized_method in {"GET", "HEAD"} else PLATFORM_AI_WRITE
    )
