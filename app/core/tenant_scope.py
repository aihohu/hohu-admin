"""Stateless helpers for tenant-owned queries, caches, and services."""

from collections.abc import Sequence
from typing import Any, Protocol

from sqlalchemy import Select, select

from app.core.tenant import TenantContext


class TenantOwnedModel(Protocol):
    tenant_id: Any


def require_tenant_context(tenant: TenantContext) -> TenantContext:
    """Fail closed when internal code tries to use an untrusted tenant value."""
    if not isinstance(tenant, TenantContext):
        raise TypeError("tenant must be a TenantContext")
    return tenant


def tenant_select[ModelT: TenantOwnedModel](
    model: type[ModelT], *, tenant: TenantContext
) -> Select:
    """Start a tenant-owned ORM query from the immutable authority boundary."""
    context = require_tenant_context(tenant)
    return select(model).where(model.tenant_id == context.tenant_id)


def tenant_filter[ModelT: TenantOwnedModel](
    model: type[ModelT], *, tenant: TenantContext
):
    """Return a tenant predicate for count/update/delete and composed queries."""
    context = require_tenant_context(tenant)
    return model.tenant_id == context.tenant_id


def tenant_values(values: dict[str, Any], *, tenant: TenantContext) -> dict[str, Any]:
    """Inject the server-owned tenant value and reject accidental overrides."""
    context = require_tenant_context(tenant)
    if "tenant_id" in values or "tenantId" in values:
        raise ValueError("tenant fields are server-owned")
    return {**values, "tenant_id": context.tenant_id}


def tenant_cache_key(tenant: TenantContext, namespace: str, *parts: str | int) -> str:
    """Build the canonical tenant namespace used by cache/lock/idempotency keys."""
    context = require_tenant_context(tenant)
    segments: Sequence[str] = (
        "tenant",
        str(context.tenant_id),
        namespace,
        *(str(part) for part in parts),
    )
    return ":".join(segments)


class TenantScopedService:
    """Mixin for stateless module-level services."""

    @staticmethod
    def scoped[ModelT: TenantOwnedModel](
        model: type[ModelT], *, tenant: TenantContext
    ) -> Select:
        return tenant_select(model, tenant=tenant)
