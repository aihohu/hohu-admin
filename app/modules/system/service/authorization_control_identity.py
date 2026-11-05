"""Current System identities exposed as immutable control-plane DTOs."""

from dataclasses import dataclass, field

from sqlalchemy import select

from app.constants import SUPER_ADMIN_ROLE_CODE
from app.core.authorization_plan import ResourceKey, ResourceKind
from app.core.exceptions import (
    AuthorizationException,
    BusinessRuleException,
    NotFoundException,
)
from app.core.tenant import TenantContext
from app.db.authorization_plan_session import _order_error
from app.modules.system.models.tenant import Tenant
from app.modules.system.service.authorization_control_audit import (
    ControlAudit as ControlAudit,
)
from app.modules.system.service.authorization_resource_catalog import resource_spec
from app.modules.system.service.authorization_system_candidate import (
    system_authorization_candidate,
)


def _freeze(value):
    if isinstance(value, dict):
        return tuple((key, _freeze(item)) for key, item in sorted(value.items()))
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class IdentitySnapshot:
    tenant: TenantContext
    actor_user_version: int
    user_name: str
    system_admin: bool
    permissions: tuple[str, ...]
    require_system: bool
    keys: tuple[ResourceKey, ...]
    _facts: tuple = field(repr=False)


async def read_identity(
    db, *, tenant, actor_user_version, permissions=(), system=False
):
    """Read without taking an earlier domain lock; the caller owns G/B."""
    if (
        type(tenant) is not TenantContext
        or type(permissions) is not tuple
        or any(type(item) is not str for item in permissions)
        or type(system) is not bool
    ):
        raise _order_error()
    actor = await system_authorization_candidate._identity(
        db, tenant, actor_user_version
    )
    facts = await system_authorization_candidate._facts(db, tenant.tenant_id)
    actor_roles = {
        row["role_id"]
        for row in facts[ResourceKind.USER_ROLES]
        if row["user_id"] == tenant.actor_user_id
    }
    enabled = {
        row["role_id"]: row
        for row in facts[ResourceKind.ROLE]
        if row["role_id"] in actor_roles and row["status"] == "1"
    }
    super_admin = any(
        row["role_code"] == SUPER_ADMIN_ROLE_CODE for row in enabled.values()
    )
    system_admin = tenant.tenant_id == 0 and super_admin
    menus = {
        row["menu_id"]
        for row in facts[ResourceKind.ROLE_MENUS]
        if row["role_id"] in enabled
    }
    granted = {
        row["permission"]
        for row in facts[ResourceKind.MENU]
        if row["menu_id"] in menus and row["status"] == "1" and row["permission"]
    }
    if system and not system_admin:
        raise AuthorizationException(
            "需要系统管理员权限", error_code="SYSTEM_ADMIN_REQUIRED"
        )
    if not super_admin and not set(permissions) <= granted:
        raise AuthorizationException("缺少操作权限", error_code="MISSING_PERMISSION")
    keys = {ResourceKey(ResourceKind.TENANT, None, (tenant.tenant_id,))}
    for kind, rows in facts.items():
        columns = resource_spec(kind).key_columns
        keys.update(
            ResourceKey(kind, tenant.tenant_id, tuple(row[name] for name in columns))
            for row in rows
        )
    return IdentitySnapshot(
        tenant=tenant,
        actor_user_version=actor_user_version,
        user_name=actor.user_name,
        system_admin=system_admin,
        permissions=permissions,
        require_system=system,
        keys=tuple(
            sorted(
                keys, key=lambda key: (key.kind.value, key.tenant_id or 0, key.identity)
            )
        ),
        _facts=_freeze({kind.value: rows for kind, rows in facts.items()}),
    )


async def assert_identity(db, snapshot):
    if type(snapshot) is not IdentitySnapshot:
        raise _order_error()
    current = await read_identity(
        db,
        tenant=snapshot.tenant,
        actor_user_version=snapshot.actor_user_version,
        permissions=snapshot.permissions,
        system=snapshot.require_system,
    )
    if current != snapshot:
        raise BusinessRuleException(
            "授权资源快照已经变化", error_code="AUTHORIZATION_SNAPSHOT_STALE"
        )
    return current


@dataclass(frozen=True, slots=True)
class PolicyTarget:
    tenant_id: int
    bootstrap_version: int
    status: str
    row_version: int
    keys: tuple[ResourceKey, ...]


async def read_policy_target(db, *, tenant_id):
    if type(tenant_id) is not int or not 0 <= tenant_id < 2**63:
        raise _order_error()
    row = (
        await db.execute(
            select(
                Tenant.tenant_id,
                Tenant.bootstrap_version,
                Tenant.status,
                Tenant.row_version,
            ).where(Tenant.tenant_id == tenant_id)
        )
    ).one_or_none()
    if row is None:
        raise NotFoundException("租户", error_code="PLATFORM_TENANT_NOT_FOUND")
    if row.bootstrap_version < 1:
        raise BusinessRuleException(
            "租户尚未完成引导", error_code="PLATFORM_TENANT_NOT_BOOTSTRAPPED"
        )
    return PolicyTarget(*row, (ResourceKey(ResourceKind.TENANT, None, (tenant_id,)),))
