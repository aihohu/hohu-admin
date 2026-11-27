"""System-owned planned bootstrap facts and writes for an existing tenant."""

from dataclasses import dataclass, field

from sqlalchemy import select

from app.constants import DATA_SCOPE_SELF, SUPER_ADMIN_ROLE_CODE, USER_ROLE_CODE
from app.core.authorization_plan import NewResource, ResourceKey, ResourceKind
from app.core.exceptions import (
    AuthorizationException,
    BusinessException,
    BusinessRuleException,
)
from app.core.id_generator import next_id
from app.modules.system.hosted_menu_seed import (
    HOSTED_PERMISSION_CODES,
    build_hosted_tenant_menus,
)
from app.modules.system.models.config import Config
from app.modules.system.models.tenant import Tenant
from app.modules.system.service.authorization_resource_catalog import resource_spec
from app.modules.system.service.authorization_system_candidate import (
    system_authorization_candidate,
)
from app.modules.system.service.authorization_tenant_candidate import (
    _target_key,
    _tenant_row,
)


def _state_error():
    return BusinessException(
        409, "租户引导状态不完整", error_code="PLATFORM_TENANT_BOOTSTRAP_STATE_INVALID"
    )


@dataclass(frozen=True, slots=True)
class SystemBootstrapPlan:
    tenant_id: int
    tenant_code: str
    lifecycle_state: str
    row_version: int
    replayed: bool
    existing: tuple
    new: tuple
    menu_count: int
    super_role_id: int | None
    records: tuple = field(repr=False)
    facts: dict = field(repr=False)
    key_hash: str = field(repr=False)
    fingerprint: str = field(repr=False)


class SystemBootstrapCandidate:
    def seeds(self, tenant_id, password_hash):
        super_id, user_role_id, admin_id = next_id(), next_id(), next_id()
        records = []
        for role_id, values in (
            (
                super_id,
                {
                    "role_name": "租户管理员",
                    "role_code": SUPER_ADMIN_ROLE_CODE,
                    "i18n_keys": {"roleName": "builtin.role.tenantAdmin.roleName"},
                },
            ),
            (
                user_role_id,
                {
                    "role_name": "普通用户",
                    "role_code": USER_ROLE_CODE,
                    "data_scope": DATA_SCOPE_SELF,
                    "role_desc": "租户内新用户默认角色",
                    "i18n_keys": {
                        "roleName": "builtin.role.user.roleName",
                        "roleDesc": "builtin.role.user.roleDesc",
                    },
                },
            ),
        ):
            records.append(
                (
                    ResourceKey(ResourceKind.ROLE, tenant_id, (role_id,)),
                    dict(tenant_id=tenant_id, role_id=role_id, status="1", **values),
                )
            )
        menus = build_hosted_tenant_menus(tenant_id)
        for menu in menus:
            values = {
                column.name: menu.__dict__[column.name]
                for column in menu.__table__.columns
                if column.name in menu.__dict__
            }
            records.append(
                (ResourceKey(ResourceKind.MENU, tenant_id, (menu.menu_id,)), values)
            )
        records.append(
            (
                ResourceKey(ResourceKind.SETTING, tenant_id, ("ai:enabled_tools",)),
                {
                    "tenant_id": tenant_id,
                    "setting_key": "ai:enabled_tools",
                    "setting_value": '["file.parse"]',
                    "status": "1",
                },
            )
        )
        records.append(
            (
                ResourceKey(ResourceKind.USER, tenant_id, (admin_id,)),
                {
                    "tenant_id": tenant_id,
                    "user_id": admin_id,
                    "user_name": "admin",
                    "nickname": "租户管理员",
                    "hashed_password": password_hash,
                    "status": "1",
                },
            )
        )
        records.append(
            (
                ResourceKey(ResourceKind.USER_ROLES, tenant_id, (admin_id, super_id)),
                {"tenant_id": tenant_id, "user_id": admin_id, "role_id": super_id},
            )
        )
        for menu in menus:
            if menu.permission in HOSTED_PERMISSION_CODES:
                records.append(
                    (
                        ResourceKey(
                            ResourceKind.ROLE_MENUS, tenant_id, (super_id, menu.menu_id)
                        ),
                        {
                            "tenant_id": tenant_id,
                            "role_id": super_id,
                            "menu_id": menu.menu_id,
                        },
                    )
                )
        return tuple(records), super_id, len(menus)

    async def prepare(self, db, *, tenant_id, key_hash, fingerprint, seeds):
        if tenant_id == 0:
            raise BusinessRuleException(
                "Default Tenant 不能引导",
                error_code="PLATFORM_DEFAULT_TENANT_IMMUTABLE",
            )
        replay = (
            (
                await db.execute(
                    select(Tenant.__table__).where(
                        Tenant.bootstrap_key_hash == key_hash
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        row = await _tenant_row(db, tenant_id)
        if replay is not None:
            if replay["tenant_id"] != tenant_id:
                raise AuthorizationException(
                    "平台目标租户不匹配", error_code="PLATFORM_TARGET_TENANT_MISMATCH"
                )
            if replay["bootstrap_fingerprint"] != fingerprint:
                raise BusinessException(
                    409,
                    "租户引导幂等键已被不同请求使用",
                    error_code="PLATFORM_TENANT_BOOTSTRAP_IDEMPOTENCY_CONFLICT",
                )
            if row["bootstrap_version"] != 1:
                raise _state_error()
        elif row["bootstrap_version"] > 0:
            raise BusinessRuleException(
                "租户已完成引导", error_code="PLATFORM_TENANT_ALREADY_BOOTSTRAPPED"
            )
        elif (
            row["status"] != "2"
            or row["lifecycle_state"] != "prepared"
            or row["bootstrap_key_hash"] is not None
            or row["bootstrap_fingerprint"] is not None
        ):
            raise _state_error()
        facts = await system_authorization_candidate._facts(db, tenant_id)
        config_exists = await db.scalar(
            select(Config.config_id).where(Config.tenant_id == tenant_id).limit(1)
        )
        if replay is None and (
            config_exists is not None
            or any(
                facts[kind]
                for kind in (
                    ResourceKind.MENU,
                    ResourceKind.ROLE,
                    ResourceKind.USER,
                    ResourceKind.SETTING,
                )
            )
        ):
            raise BusinessException(
                409,
                "prepared tenant 存在未受控初始化数据",
                error_code="PLATFORM_TENANT_BOOTSTRAP_DIRTY",
            )
        menu_count = len(facts[ResourceKind.MENU])
        if replay is not None and (
            menu_count == 0
            or len(facts[ResourceKind.ROLE]) != 2
            or sum(row["user_name"] == "admin" for row in facts[ResourceKind.USER]) != 1
        ):
            raise _state_error()
        existing = [_target_key(tenant_id)]
        for kind, rows in facts.items():
            existing.extend(
                ResourceKey(
                    kind,
                    tenant_id,
                    tuple(item[name] for name in resource_spec(kind).key_columns),
                )
                for item in rows
            )
        records, super_id, seeded_menu_count = seeds
        return SystemBootstrapPlan(
            tenant_id,
            row["tenant_code"],
            row["lifecycle_state"],
            row["row_version"],
            replay is not None,
            tuple(existing),
            () if replay is not None else tuple(NewResource(key) for key, _ in records),
            menu_count if replay is not None else seeded_menu_count,
            None if replay is not None else super_id,
            () if replay is not None else records,
            facts,
            key_hash,
            fingerprint,
        )

    async def apply(self, tx, plan):
        if plan.replayed:
            return
        for key, values in plan.records:
            await tx.insert_reserved(key, values)
        await tx.update_held(
            _target_key(plan.tenant_id),
            {
                "bootstrap_key_hash": plan.key_hash,
                "bootstrap_fingerprint": plan.fingerprint,
                "bootstrap_version": 1,
            },
            expected={"row_version": plan.row_version, "bootstrap_version": 0},
        )


system_bootstrap_candidate = SystemBootstrapCandidate()
