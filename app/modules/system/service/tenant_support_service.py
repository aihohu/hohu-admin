"""Minimized platform support queries and guarded tenant audit retention."""

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import PageResult
from app.core.config import settings
from app.core.exceptions import (
    AuthorizationException,
    BusinessException,
    BusinessRuleException,
    NotFoundException,
)
from app.core.tenant import PlatformContext, require_platform_permission
from app.modules.platform.constants import (
    PLATFORM_AUDIT_RETENTION,
    PLATFORM_SUPPORT_READ,
)
from app.modules.system.models.login_log import SysLoginLog
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.models.tenant import Tenant


@dataclass(frozen=True, slots=True)
class SupportAuditProjection:
    event_id: int
    category: str
    event_type: str
    outcome: str
    duration_ms: int | None
    occurred_at: datetime


@dataclass(frozen=True, slots=True)
class RetentionPreview:
    cutoff: datetime
    operation_count: int
    login_count: int

    @property
    def affected_count(self) -> int:
        return self.operation_count + self.login_count


def _require_target(platform: PlatformContext, tenant_id: int) -> None:
    if platform.target_tenant_id != tenant_id:
        raise AuthorizationException(
            "平台目标租户不匹配",
            error_code="PLATFORM_TARGET_TENANT_MISMATCH",
        )


def _normalize_cutoff(cutoff: datetime) -> datetime:
    if not isinstance(cutoff, datetime):
        raise BusinessRuleException(
            "审计保留截止时间无效",
            error_code="PLATFORM_RETENTION_WINDOW_INVALID",
        )
    if cutoff.tzinfo is not None:
        cutoff = cutoff.astimezone().replace(tzinfo=None)
    latest_allowed = datetime.now() - timedelta(
        days=settings.PLATFORM_AUDIT_MIN_RETENTION_DAYS
    )
    if cutoff > latest_allowed:
        raise BusinessRuleException(
            "审计保留截止时间过新",
            error_code="PLATFORM_RETENTION_WINDOW_INVALID",
        )
    return cutoff


async def _require_tenant(db: AsyncSession, tenant_id: int) -> None:
    exists = await db.scalar(
        select(Tenant.tenant_id).where(Tenant.tenant_id == tenant_id)
    )
    if exists is None:
        raise NotFoundException("租户", error_code="PLATFORM_TENANT_NOT_FOUND")


class TenantSupportService:
    async def list_operation_logs(
        self,
        db: AsyncSession,
        *,
        tenant_id: int,
        current: int,
        size: int,
        platform: PlatformContext,
    ) -> PageResult:
        require_platform_permission(platform, PLATFORM_SUPPORT_READ)
        _require_target(platform, tenant_id)
        await _require_tenant(db, tenant_id)
        filters = (
            SysOperationLog.tenant_id == tenant_id,
            SysOperationLog.audit_scope == "tenant",
        )
        total = (
            await db.scalar(
                select(func.count()).select_from(SysOperationLog).where(*filters)
            )
            or 0
        )
        rows = (
            await db.execute(
                select(
                    SysOperationLog.operation_log_id,
                    SysOperationLog.module,
                    SysOperationLog.action,
                    SysOperationLog.status_code,
                    SysOperationLog.duration,
                    SysOperationLog.create_time,
                )
                .where(*filters)
                .order_by(
                    SysOperationLog.create_time.desc(),
                    SysOperationLog.operation_log_id.desc(),
                )
                .offset((current - 1) * size)
                .limit(size)
            )
        ).all()
        records = [
            SupportAuditProjection(
                event_id=row.operation_log_id,
                category=row.module,
                event_type=row.action,
                outcome=str(row.status_code or "unknown"),
                duration_ms=row.duration,
                occurred_at=row.create_time,
            )
            for row in rows
        ]
        return PageResult(records=records, total=total, current=current, size=size)

    async def list_login_logs(
        self,
        db: AsyncSession,
        *,
        tenant_id: int,
        current: int,
        size: int,
        platform: PlatformContext,
    ) -> PageResult:
        require_platform_permission(platform, PLATFORM_SUPPORT_READ)
        _require_target(platform, tenant_id)
        await _require_tenant(db, tenant_id)
        filters = (
            SysLoginLog.tenant_id == tenant_id,
            SysLoginLog.audit_scope == "tenant",
        )
        total = (
            await db.scalar(
                select(func.count()).select_from(SysLoginLog).where(*filters)
            )
            or 0
        )
        rows = (
            await db.execute(
                select(
                    SysLoginLog.login_log_id,
                    SysLoginLog.status,
                    SysLoginLog.login_time,
                )
                .where(*filters)
                .order_by(
                    SysLoginLog.login_time.desc(),
                    SysLoginLog.login_log_id.desc(),
                )
                .offset((current - 1) * size)
                .limit(size)
            )
        ).all()
        event_types = {
            "1": "login_succeeded",
            "2": "login_failed",
            "3": "login_locked",
        }
        records = [
            SupportAuditProjection(
                event_id=row.login_log_id,
                category="authentication",
                event_type=event_types.get(row.status, "login_unknown"),
                outcome=event_types.get(row.status, "login_unknown"),
                duration_ms=None,
                occurred_at=row.login_time,
            )
            for row in rows
        ]
        return PageResult(records=records, total=total, current=current, size=size)

    async def preview_retention(
        self,
        db: AsyncSession,
        *,
        tenant_id: int,
        cutoff: datetime,
        platform: PlatformContext,
    ) -> RetentionPreview:
        require_platform_permission(platform, PLATFORM_AUDIT_RETENTION)
        _require_target(platform, tenant_id)
        cutoff = _normalize_cutoff(cutoff)
        await _require_tenant(db, tenant_id)
        return await self._count_retention(db, tenant_id=tenant_id, cutoff=cutoff)

    async def purge_retention(
        self,
        db: AsyncSession,
        *,
        tenant_id: int,
        cutoff: datetime,
        expected_operation_count: int,
        expected_login_count: int,
        platform: PlatformContext,
    ) -> RetentionPreview:
        require_platform_permission(platform, PLATFORM_AUDIT_RETENTION)
        _require_target(platform, tenant_id)
        cutoff = _normalize_cutoff(cutoff)
        if expected_operation_count < 0 or expected_login_count < 0:
            raise BusinessRuleException(
                "预期删除数量无效",
                error_code="PLATFORM_RETENTION_EXPECTED_COUNT_INVALID",
            )
        await _require_tenant(db, tenant_id)
        await db.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"platform-audit-retention:{tenant_id}"},
        )
        actual = await self._count_retention(db, tenant_id=tenant_id, cutoff=cutoff)
        if (
            actual.operation_count != expected_operation_count
            or actual.login_count != expected_login_count
        ):
            raise BusinessException(
                code=409,
                message="审计 retention preview 已过期",
                error_code="PLATFORM_RETENTION_PREVIEW_STALE",
            )
        operation_result = await db.execute(
            delete(SysOperationLog).where(
                SysOperationLog.tenant_id == tenant_id,
                SysOperationLog.audit_scope == "tenant",
                SysOperationLog.create_time < cutoff,
            )
        )
        login_result = await db.execute(
            delete(SysLoginLog).where(
                SysLoginLog.tenant_id == tenant_id,
                SysLoginLog.audit_scope == "tenant",
                SysLoginLog.login_time < cutoff,
            )
        )
        if (
            operation_result.rowcount != actual.operation_count
            or login_result.rowcount != actual.login_count
        ):
            raise BusinessException(
                code=409,
                message="审计 retention 数据在删除期间发生变化",
                error_code="PLATFORM_RETENTION_PREVIEW_STALE",
            )
        await db.flush()
        return actual

    async def _count_retention(
        self, db: AsyncSession, *, tenant_id: int, cutoff: datetime
    ) -> RetentionPreview:
        operation_count = (
            await db.scalar(
                select(func.count())
                .select_from(SysOperationLog)
                .where(
                    SysOperationLog.tenant_id == tenant_id,
                    SysOperationLog.audit_scope == "tenant",
                    SysOperationLog.create_time < cutoff,
                )
            )
            or 0
        )
        login_count = (
            await db.scalar(
                select(func.count())
                .select_from(SysLoginLog)
                .where(
                    SysLoginLog.tenant_id == tenant_id,
                    SysLoginLog.audit_scope == "tenant",
                    SysLoginLog.login_time < cutoff,
                )
            )
            or 0
        )
        return RetentionPreview(
            cutoff=cutoff,
            operation_count=operation_count,
            login_count=login_count,
        )


tenant_support_service = TenantSupportService()
