"""Bounded, explicit System audit retention under a Principal proof."""

from sqlalchemy import select

from app.core.authorization_plan import (
    LockMode,
    LockPlan,
    ResourceKey,
    ResourceKind,
    TenantLock,
    TransactionBudget,
)
from app.core.exceptions import BusinessException
from app.db.authorization_plan_session import _order_error
from app.db.authorization_system_owner import require_candidate_session
from app.modules.platform.authorization_principal_audit import PrincipalAudit
from app.modules.platform.authorization_principal_candidate import (
    assert_principal,
    read_principal,
)
from app.modules.system.models.login_log import SysLoginLog
from app.modules.system.models.operation_log import SysOperationLog
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_tenant_candidate import (
    _target_key,
    _tenant_row,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.tenant_support_service import (
    RetentionPreview,
    _normalize_cutoff,
)


async def _rows(db, tenant_id, cutoff):
    result = []
    count = 0
    for kind, model, time_column, id_column in (
        (
            ResourceKind.SYSTEM_OPERATION_LOG,
            SysOperationLog,
            SysOperationLog.create_time,
            SysOperationLog.operation_log_id,
        ),
        (
            ResourceKind.LOGIN_LOG,
            SysLoginLog,
            SysLoginLog.login_time,
            SysLoginLog.login_log_id,
        ),
    ):
        rows = tuple(
            dict(row)
            for row in (
                await db.execute(
                    select(model.__table__)
                    .where(
                        model.tenant_id == tenant_id,
                        model.audit_scope == "tenant",
                        time_column < cutoff,
                    )
                    .order_by(id_column)
                    .limit(10001)
                )
            ).mappings()
        )
        count += len(rows)
        if count > 10000:
            raise BusinessException(
                422,
                "日志保留完整计划超过上限",
                error_code="AUTHORIZATION_PLAN_LIMIT_EXCEEDED",
            )
        result.append((kind, id_column.key, rows))
    return tuple(result)


class RetentionAuthorizationCandidate:
    async def execute(
        self,
        db,
        *,
        identity,
        target_tenant_id,
        cutoff,
        audit,
        purge=False,
        expected_operation_count=0,
        expected_login_count=0,
    ):
        require_candidate_session(db)
        if (
            type(target_tenant_id) is not int
            or not 0 <= target_tenant_id < 2**63
            or type(purge) is not bool
            or type(audit) is not PrincipalAudit
        ):
            raise _order_error()
        if any(
            type(value) is not int or value < 0
            for value in (expected_operation_count, expected_login_count)
        ):
            raise BusinessException(
                422,
                "预期删除数量无效",
                error_code="PLATFORM_RETENTION_EXPECTED_COUNT_INVALID",
            )
        cutoff = _normalize_cutoff(cutoff)
        operation = "retention.purge" if purge else "retention.preview"
        audit.assert_request(
            identity=identity,
            permission="platform:audit:retention",
            target_tenant_id=target_tenant_id,
            operation=operation,
        )
        with bounded_authorization_reads(db):
            async with authorization_transactions.scope(
                db,
                tenants=(
                    TenantLock(
                        target_tenant_id,
                        LockMode.EXCLUSIVE if purge else LockMode.SHARED,
                    ),
                ),
                budget=TransactionBudget(timeout_ms=60000 if purge else 8000),
            ) as tx:
                principal = await read_principal(
                    db, identity, "platform:audit:retention"
                )
                await _tenant_row(db, target_tenant_id)
                facts = await _rows(db, target_tenant_id, cutoff)
                keys = tuple(
                    ResourceKey(kind, target_tenant_id, (row[column],))
                    for kind, column, rows in facts
                    for row in rows
                )
                await tx.lock(
                    LockPlan(
                        existing=tuple(
                            set(
                                principal.keys
                                + audit.existing
                                + (_target_key(target_tenant_id),)
                                + keys
                            )
                        ),
                        new=audit.new,
                    )
                )
                await assert_principal(db, principal)
                if await _rows(db, target_tenant_id, cutoff) != facts:
                    raise BusinessException(
                        409,
                        "审计 retention preview 已过期",
                        error_code="PLATFORM_RETENTION_PREVIEW_STALE",
                    )
                counts = (len(facts[0][2]), len(facts[1][2]))
                if purge:
                    if counts != (expected_operation_count, expected_login_count):
                        raise BusinessException(
                            409,
                            "审计 retention preview 已过期",
                            error_code="PLATFORM_RETENTION_PREVIEW_STALE",
                        )
                    for kind, column, rows in facts:
                        for row in rows:
                            await tx.delete_retained(
                                ResourceKey(kind, target_tenant_id, (row[column],)),
                                expected=row,
                            )
                await audit.append(
                    tx, {"affectedCount" if purge else "recordCount": sum(counts)}
                )
                return RetentionPreview(cutoff, *counts)


retention_authorization_candidate = RetentionAuthorizationCandidate()
