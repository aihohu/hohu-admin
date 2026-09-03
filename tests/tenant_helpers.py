"""Explicit trusted tenant fixtures shared by tenant-aware tests."""

from app.core.id_generator import next_id
from app.core.tenant import TenantContext, bind_tenant_context
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User


def tenant_context(*, tenant_id: int = 0, actor_user_id: int = 1) -> TenantContext:
    code = "default" if tenant_id == 0 else f"tenant-{tenant_id}"
    return TenantContext(
        tenant_id=tenant_id,
        tenant_code=code,
        actor_user_id=actor_user_id,
        tenant_version=1,
        source="access_token",
    )


def bind_test_user(user: User) -> TenantContext:
    tenant = tenant_context(
        tenant_id=int(user.tenant_id), actor_user_id=int(user.user_id)
    )
    bind_tenant_context(user, tenant)
    return tenant


async def create_test_tenant(db, *, prefix: str) -> Tenant:
    """Create one enabled non-default tenant inside the caller's test transaction."""
    marker = next_id()
    tenant = Tenant(
        tenant_id=marker,
        tenant_code=f"{prefix}-{marker}",
        tenant_name=f"{prefix} {marker}",
        status="1",
        lifecycle_state="active",
        row_version=1,
    )
    db.add(tenant)
    await db.flush()
    return tenant
