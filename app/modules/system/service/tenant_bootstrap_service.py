"""System-owned half of prepared tenant bootstrap."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import (
    DATA_SCOPE_SELF,
    STATUS_DISABLED,
    STATUS_ENABLED,
    SUPER_ADMIN_ROLE_CODE,
    USER_ROLE_CODE,
)
from app.core.exceptions import (
    AuthorizationException,
    BusinessException,
    BusinessRuleException,
    NotFoundException,
)
from app.core.security import get_password_hash
from app.core.tenant import (
    DEFAULT_TENANT_ID,
    PlatformContext,
    require_platform_permission,
)
from app.db.base import role_menus, user_roles
from app.modules.platform.constants import PLATFORM_TENANT_BOOTSTRAP
from app.modules.system.hosted_menu_seed import (
    HOSTED_PERMISSION_CODES,
    build_hosted_tenant_menus,
)
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User
from app.utils.validators import validate_password


@dataclass(frozen=True, slots=True)
class SystemTenantBootstrapSummary:
    menu_count: int
    role_count: int
    admin_username: str = "admin"
    super_role_id: int | None = None


def _require_target(platform: PlatformContext, tenant_id: int) -> None:
    if platform.target_tenant_id != tenant_id:
        raise AuthorizationException(
            "平台目标租户不匹配",
            error_code="PLATFORM_TARGET_TENANT_MISMATCH",
        )


async def _bootstrap_locks(db: AsyncSession, *, tenant_id: int, key_hash: str) -> None:
    statement = text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))")
    for lock_key in sorted(
        {
            f"platform-tenant-bootstrap-key:{key_hash}",
            f"platform-tenant-bootstrap-target:{tenant_id}",
        }
    ):
        await db.execute(statement, {"lock_key": lock_key})


class SystemTenantBootstrapService:
    async def lock_target(
        self,
        db: AsyncSession,
        *,
        tenant_id: int,
        key_hash: str,
        fingerprint: str,
        platform: PlatformContext,
    ) -> tuple[Tenant, bool]:
        require_platform_permission(platform, PLATFORM_TENANT_BOOTSTRAP)
        _require_target(platform, tenant_id)
        await _bootstrap_locks(db, tenant_id=tenant_id, key_hash=key_hash)

        replay = await db.scalar(
            select(Tenant).where(Tenant.bootstrap_key_hash == key_hash)
        )
        if replay is not None:
            if replay.tenant_id != tenant_id:
                raise AuthorizationException(
                    "平台目标租户不匹配",
                    error_code="PLATFORM_TARGET_TENANT_MISMATCH",
                )
            if replay.bootstrap_fingerprint != fingerprint:
                raise BusinessException(
                    code=409,
                    message="Idempotency-Key 已绑定其他租户引导请求",
                    error_code=("PLATFORM_TENANT_BOOTSTRAP_IDEMPOTENCY_CONFLICT"),
                )
            if replay.bootstrap_version != 1:
                raise BusinessException(
                    code=409,
                    message="租户引导状态不完整",
                    error_code="PLATFORM_TENANT_BOOTSTRAP_STATE_INVALID",
                )
            return replay, True

        if tenant_id == DEFAULT_TENANT_ID:
            raise BusinessRuleException(
                "Default Tenant 不执行 hosted bootstrap",
                error_code="PLATFORM_DEFAULT_TENANT_IMMUTABLE",
            )
        tenant = await db.scalar(
            select(Tenant).where(Tenant.tenant_id == tenant_id).with_for_update()
        )
        if tenant is None:
            raise NotFoundException("租户", error_code="PLATFORM_TENANT_NOT_FOUND")
        if tenant.bootstrap_version > 0:
            raise BusinessException(
                code=409,
                message="租户已经完成引导",
                error_code="PLATFORM_TENANT_ALREADY_BOOTSTRAPPED",
            )
        if tenant.lifecycle_state != "prepared" or tenant.status != STATUS_DISABLED:
            raise BusinessRuleException(
                "仅 prepared tenant 可以执行引导",
                error_code="PLATFORM_TENANT_NOT_PREPARED",
            )
        if (
            tenant.bootstrap_key_hash is not None
            or tenant.bootstrap_fingerprint is not None
        ):
            raise BusinessException(
                code=409,
                message="租户引导状态不完整",
                error_code="PLATFORM_TENANT_BOOTSTRAP_STATE_INVALID",
            )
        await self._assert_clean(db, tenant_id=tenant_id)
        return tenant, False

    @staticmethod
    async def _assert_clean(db: AsyncSession, *, tenant_id: int) -> None:
        counts = []
        for model in (Menu, Role, User):
            counts.append(
                await db.scalar(
                    select(func.count())
                    .select_from(model)
                    .where(model.tenant_id == tenant_id)
                )
                or 0
            )
        if any(counts):
            raise BusinessException(
                code=409,
                message="prepared tenant 存在未受控初始化数据",
                error_code="PLATFORM_TENANT_BOOTSTRAP_DIRTY",
            )

    async def seed(
        self,
        db: AsyncSession,
        *,
        tenant: Tenant,
        admin_password: str,
        platform: PlatformContext,
    ) -> SystemTenantBootstrapSummary:
        require_platform_permission(platform, PLATFORM_TENANT_BOOTSTRAP)
        _require_target(platform, tenant.tenant_id)
        validate_password(admin_password)
        menus = build_hosted_tenant_menus(tenant.tenant_id)
        db.add_all(menus)
        await db.flush()

        super_role = Role(
            tenant_id=tenant.tenant_id,
            role_name="超级管理员",
            role_code=SUPER_ADMIN_ROLE_CODE,
            status=STATUS_ENABLED,
        )
        user_role = Role(
            tenant_id=tenant.tenant_id,
            role_name="普通用户",
            role_code=USER_ROLE_CODE,
            role_desc="租户内新用户默认角色",
            data_scope=DATA_SCOPE_SELF,
            status=STATUS_ENABLED,
        )
        db.add_all([super_role, user_role])
        await db.flush()
        permission_menus = [
            menu for menu in menus if menu.permission in HOSTED_PERMISSION_CODES
        ]
        await db.execute(
            insert(role_menus),
            [
                {
                    "tenant_id": tenant.tenant_id,
                    "role_id": super_role.role_id,
                    "menu_id": menu.menu_id,
                }
                for menu in permission_menus
            ],
        )

        admin = User(
            tenant_id=tenant.tenant_id,
            user_name="admin",
            nickname="租户管理员",
            hashed_password=get_password_hash(admin_password),
            status=STATUS_ENABLED,
        )
        db.add(admin)
        await db.flush()
        await db.execute(
            insert(user_roles).values(
                tenant_id=tenant.tenant_id,
                user_id=admin.user_id,
                role_id=super_role.role_id,
            )
        )
        return SystemTenantBootstrapSummary(
            menu_count=len(menus),
            role_count=2,
            super_role_id=super_role.role_id,
        )

    @staticmethod
    async def summarize(
        db: AsyncSession, *, tenant_id: int, platform: PlatformContext
    ) -> SystemTenantBootstrapSummary:
        require_platform_permission(platform, PLATFORM_TENANT_BOOTSTRAP)
        _require_target(platform, tenant_id)
        menu_count = (
            await db.scalar(
                select(func.count())
                .select_from(Menu)
                .where(Menu.tenant_id == tenant_id)
            )
            or 0
        )
        role_count = (
            await db.scalar(
                select(func.count())
                .select_from(Role)
                .where(Role.tenant_id == tenant_id)
            )
            or 0
        )
        admin_count = (
            await db.scalar(
                select(func.count())
                .select_from(User)
                .where(User.tenant_id == tenant_id, User.user_name == "admin")
            )
            or 0
        )
        if menu_count == 0 or role_count != 2 or admin_count != 1:
            raise BusinessException(
                code=409,
                message="租户引导状态不完整",
                error_code="PLATFORM_TENANT_BOOTSTRAP_STATE_INVALID",
            )
        return SystemTenantBootstrapSummary(
            menu_count=menu_count,
            role_count=role_count,
        )

    @staticmethod
    async def complete(
        db: AsyncSession,
        *,
        tenant: Tenant,
        key_hash: str,
        fingerprint: str,
        platform: PlatformContext,
    ) -> None:
        require_platform_permission(platform, PLATFORM_TENANT_BOOTSTRAP)
        _require_target(platform, tenant.tenant_id)
        tenant.bootstrap_key_hash = key_hash
        tenant.bootstrap_fingerprint = fingerprint
        tenant.bootstrap_version = 1
        await db.flush()
        await db.refresh(tenant)


system_tenant_bootstrap_service = SystemTenantBootstrapService()
