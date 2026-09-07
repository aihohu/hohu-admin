"""Platform-only lifecycle operations for the global tenant registry."""

import hashlib
import json
import re

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import PageResult
from app.core.exceptions import (
    AuthorizationException,
    BusinessException,
    BusinessRuleException,
    NotFoundException,
)
from app.core.tenant import (
    DEFAULT_TENANT_CODE,
    DEFAULT_TENANT_ID,
    PlatformContext,
    normalize_tenant_code,
    require_platform_permission,
)
from app.modules.platform.constants import (
    PLATFORM_AI_READ,
    PLATFORM_AI_WRITE,
    PLATFORM_TENANT_READ,
    PLATFORM_TENANT_WRITE,
)
from app.modules.system.models.tenant import Tenant

_IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


def hash_tenant_idempotency_key(value: str) -> str:
    """Return the only representation of an idempotency key stored in the DB."""
    if not isinstance(value, str) or _IDEMPOTENCY_KEY_RE.fullmatch(value) is None:
        raise BusinessRuleException(
            "Idempotency-Key 格式无效",
            error_code="PLATFORM_TENANT_IDEMPOTENCY_KEY_INVALID",
        )
    return hashlib.sha256(value.encode()).hexdigest()


def _tenant_fingerprint(*, tenant_code: str, tenant_name: str) -> str:
    payload = json.dumps(
        {"tenantCode": tenant_code, "tenantName": tenant_name},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _require_target(platform: PlatformContext, tenant_id: int) -> None:
    if platform.target_tenant_id != tenant_id:
        raise AuthorizationException(
            "平台目标租户不匹配",
            error_code="PLATFORM_TARGET_TENANT_MISMATCH",
        )


async def _lock_prepare_keys(
    db: AsyncSession, *, tenant_code: str, key_hash: str
) -> None:
    statement = text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))")
    for lock_key in sorted(
        {f"platform-tenant-code:{tenant_code}", f"platform-tenant-key:{key_hash}"}
    ):
        await db.execute(statement, {"lock_key": lock_key})


class TenantLifecycleService:
    async def require_ai_policy_target(
        self,
        db: AsyncSession,
        *,
        tenant_id: int,
        write: bool,
        platform: PlatformContext,
    ) -> Tenant:
        """Resolve a bootstrapped tenant for platform AI policy administration."""
        require_platform_permission(
            platform, PLATFORM_AI_WRITE if write else PLATFORM_AI_READ
        )
        _require_target(platform, tenant_id)
        statement = select(Tenant).where(Tenant.tenant_id == tenant_id)
        if write:
            statement = statement.with_for_update()
        tenant = await db.scalar(statement)
        if tenant is None:
            raise NotFoundException("租户", error_code="PLATFORM_TENANT_NOT_FOUND")
        if tenant.bootstrap_version < 1:
            raise BusinessRuleException(
                "租户尚未完成引导",
                error_code="PLATFORM_TENANT_NOT_BOOTSTRAPPED",
            )
        return tenant

    async def list_tenants(
        self,
        db: AsyncSession,
        *,
        current: int,
        size: int,
        platform: PlatformContext,
    ) -> PageResult:
        require_platform_permission(platform, PLATFORM_TENANT_READ)
        total = await db.scalar(select(func.count()).select_from(Tenant)) or 0
        records = (
            (
                await db.execute(
                    select(Tenant)
                    .order_by(Tenant.created_at.desc(), Tenant.tenant_id.desc())
                    .offset((current - 1) * size)
                    .limit(size)
                )
            )
            .scalars()
            .all()
        )
        return PageResult(records=records, total=total, current=current, size=size)

    async def get_tenant(
        self,
        db: AsyncSession,
        *,
        tenant_id: int,
        platform: PlatformContext,
    ) -> Tenant:
        require_platform_permission(platform, PLATFORM_TENANT_READ)
        _require_target(platform, tenant_id)
        tenant = await db.scalar(select(Tenant).where(Tenant.tenant_id == tenant_id))
        if tenant is None:
            raise NotFoundException("租户", error_code="PLATFORM_TENANT_NOT_FOUND")
        return tenant

    async def prepare_tenant(
        self,
        db: AsyncSession,
        *,
        tenant_id: int,
        tenant_code: str,
        tenant_name: str,
        idempotency_key: str,
        platform: PlatformContext,
    ) -> Tenant:
        require_platform_permission(platform, PLATFORM_TENANT_WRITE)
        _require_target(platform, tenant_id)
        normalized_code = normalize_tenant_code(tenant_code)
        normalized_name = tenant_name.strip() if isinstance(tenant_name, str) else ""
        if (
            normalized_code is None
            or len(normalized_code) < 2
            or normalized_code == DEFAULT_TENANT_CODE
        ):
            raise BusinessRuleException(
                "租户代码无效", error_code="PLATFORM_TENANT_CODE_INVALID"
            )
        if (
            not normalized_name
            or len(normalized_name) > 100
            or any(not character.isprintable() for character in normalized_name)
        ):
            raise BusinessRuleException(
                "租户名称无效", error_code="PLATFORM_TENANT_NAME_INVALID"
            )
        key_hash = hash_tenant_idempotency_key(idempotency_key)
        fingerprint = _tenant_fingerprint(
            tenant_code=normalized_code,
            tenant_name=normalized_name,
        )
        await _lock_prepare_keys(db, tenant_code=normalized_code, key_hash=key_hash)

        replay = await db.scalar(
            select(Tenant).where(Tenant.provisioning_key_hash == key_hash)
        )
        if replay is not None:
            if replay.provisioning_fingerprint != fingerprint:
                raise BusinessException(
                    code=409,
                    message="Idempotency-Key 已绑定其他租户请求",
                    error_code="PLATFORM_TENANT_IDEMPOTENCY_CONFLICT",
                )
            if replay.tenant_id != tenant_id:
                raise AuthorizationException(
                    "平台目标租户不匹配",
                    error_code="PLATFORM_TARGET_TENANT_MISMATCH",
                )
            return replay

        existing_code = await db.scalar(
            select(Tenant.tenant_id).where(Tenant.tenant_code == normalized_code)
        )
        if existing_code is not None:
            raise BusinessException(
                code=409,
                message="租户代码已存在",
                error_code="PLATFORM_TENANT_CODE_EXISTS",
            )

        tenant = Tenant(
            tenant_id=tenant_id,
            tenant_code=normalized_code,
            tenant_name=normalized_name,
            status="2",
            lifecycle_state="prepared",
            provisioning_key_hash=key_hash,
            provisioning_fingerprint=fingerprint,
            row_version=1,
        )
        db.add(tenant)
        await db.flush()
        return tenant

    async def disable_tenant(
        self,
        db: AsyncSession,
        *,
        tenant_id: int,
        platform: PlatformContext,
    ) -> Tenant:
        require_platform_permission(platform, PLATFORM_TENANT_WRITE)
        _require_target(platform, tenant_id)
        if tenant_id == DEFAULT_TENANT_ID:
            raise BusinessRuleException(
                "Default Tenant 不能通过平台 API 禁用",
                error_code="PLATFORM_DEFAULT_TENANT_IMMUTABLE",
            )
        tenant = await db.scalar(
            select(Tenant).where(Tenant.tenant_id == tenant_id).with_for_update()
        )
        if tenant is None:
            raise NotFoundException("租户", error_code="PLATFORM_TENANT_NOT_FOUND")
        if tenant.lifecycle_state == "disabled" and tenant.status == "2":
            return tenant
        tenant.status = "2"
        tenant.lifecycle_state = "disabled"
        await db.flush()
        await db.refresh(tenant)
        return tenant


tenant_lifecycle_service = TenantLifecycleService()
