"""Internal capacity settings, never a grant of business or data authority."""

import json
from types import MappingProxyType

from sqlalchemy import select

from app.core.authorization_plan import LockPlan, ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.core.tenant import TenantContext
from app.db.authorization_plan_session import _order_error
from app.modules.system.models.setting import SystemSetting
from app.modules.system.models.tenant import Tenant

DEFAULTS = MappingProxyType(
    {
        "ai:rate_limit:user_write_per_min": 20,
        "ai:quota:daily_per_user": 2000,
        "ai:limit:tool_timeout_sec": 10,
        "ai:rate_limit:global_per_min": 0,
        "ai:budget:conv_per_day": 0,
        "ai:failures:threshold": 2,
        "ai:failures:ttl_sec": 600,
        "ai:auto_disable:perm_denied_per_hour": 50,
        "ai:ip_allowlist": (),
        "ai:auto_disable:injection_per_hour": 5,
        "ai:auto_disable:duration_sec": 86400,
    }
)


class AuxiliaryCandidate:
    async def options(self, db, tx, *, tenant):
        tx.assert_session(db)
        if type(tenant) is not TenantContext:
            raise _order_error()
        rows = list(
            await db.scalars(
                select(SystemSetting)
                .where(
                    SystemSetting.tenant_id == tenant.tenant_id,
                    SystemSetting.setting_key.in_(DEFAULTS),
                )
                .limit(len(DEFAULTS) + 1)
            )
        )
        if len(rows) > len(DEFAULTS) or any(
            len(row.setting_value.encode()) > 2048 for row in rows
        ):
            raise _order_error()
        before = {row.setting_key: (row.setting_value, row.status) for row in rows}
        await tx.lock(
            LockPlan(
                existing=(
                    ResourceKey(K.TENANT, None, tenant.tenant_id),
                    *(
                        ResourceKey(K.SETTING, tenant.tenant_id, row.setting_key)
                        for row in rows
                    ),
                )
            )
        )
        current = await db.get(Tenant, tenant.tenant_id, populate_existing=True)
        if (
            current is None
            or current.tenant_code != tenant.tenant_code
            or current.row_version != tenant.tenant_version
            or current.status != "1"
            or current.lifecycle_state != "active"
        ):
            raise _order_error()
        values = dict(DEFAULTS)
        for key, expected in before.items():
            row = await db.scalar(
                select(SystemSetting)
                .where(
                    SystemSetting.tenant_id == tenant.tenant_id,
                    SystemSetting.setting_key == key,
                )
                .execution_options(populate_existing=True)
            )
            if row is None or (row.setting_value, row.status) != expected:
                raise _order_error()
            if row.status != "1":
                continue
            try:
                if key == "ai:ip_allowlist":
                    decoded = json.loads(row.setting_value)
                    if (
                        type(decoded) is not list
                        or len(decoded) > 256
                        or any(
                            type(item) is not str or len(item) > 128 for item in decoded
                        )
                    ):
                        raise ValueError
                    values[key] = tuple(decoded)
                else:
                    values[key] = int(row.setting_value)
            except (ValueError, TypeError):
                values[key] = DEFAULTS[key]
        return MappingProxyType(values)


auxiliary_candidate = AuxiliaryCandidate()
