"""ImportState 状态机 + CAS helper + 清理 cron（v2.2 P0/P1）。

spec §2.26 / §2.27 / §2.29 + spec §2.31 ExportTask 状态机。

CAS helper 用 sqlalchemy.Table 抽象写 raw UPDATE（spec §2.26 反例 3：
ORM 的 synchronize_session 容易引入意外），同时避免循环依赖 ORM。
Task 2 ORM 落地后仍可用本 Table 路径（CAS rowcount 精确性是核心需求）。
"""

from typing import Any

from sqlalchemy import Column, MetaData, String, Table, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessRuleException
from app.modules.system.user.constants import (
    LEGAL_TRANSITIONS,
    ImportBatchStatus,
)

_batch_metadata = MetaData()
_batch_table = Table(
    "sys_user_import_batch",
    _batch_metadata,
    Column("batch_id", String(64), primary_key=True),
    Column("status", String(32), nullable=False),
    extend_existing=True,
)


def validate_transition(
    from_status: ImportBatchStatus,
    to_status: ImportBatchStatus,
) -> None:
    """校验状态转换合法性，非法转换抛 AI_IMPORT_ILLEGAL_TRANSITION。

    spec §2.26 集中状态机 + §2.27 CAS 幂等基础：调用方在写 DB 前先校验，
    让非法业务逻辑（如终态 → 任意态）在 service 层就被拒绝，避免脏 UPDATE。
    """
    if to_status not in LEGAL_TRANSITIONS.get(from_status, frozenset()):
        raise BusinessRuleException(
            f"非法状态转换 {from_status.value} → {to_status.value}",
            error_code="AI_IMPORT_ILLEGAL_TRANSITION",
        )


async def _transition_batch_status(
    db: AsyncSession,
    batch_id: str,
    from_status: ImportBatchStatus,
    to_status: ImportBatchStatus,
    **updates: Any,
) -> bool:
    """CAS 状态转换，防并发覆盖（spec §2.27 幂等基础）。

    Returns:
        True: 转换成功（rowcount=1）
        False: from_status 不匹配（rowcount=0），调用方按业务语义处理
              （如 spec §2.27 重放场景视为幂等成功）
    """
    validate_transition(from_status, to_status)
    set_values = {**updates, "status": to_status.value}
    result = await db.execute(
        update(_batch_table)
        .where(
            _batch_table.c.batch_id == batch_id,
            _batch_table.c.status == from_status.value,
        )
        .values(**set_values)
    )
    return result.rowcount == 1


async def cleanup_expired_batches(*args: Any, **kwargs: Any) -> None:
    """清理 90 天前已结束 batch + 关联 failed_rows 文件 + batch_log。

    Task 22 落地：每日 02:00 cron，spec §10 Task 22。
    """


def validate_reason_consistency(
    preview_reason: str,
    execute_reason: str,
) -> None:
    """校验 preview 与 execute 阶段 reason 一致（spec §2.30 v2.2 P1-3）。

    防止用户 preview 时填「HR 同步」，execute 时填「ERP 推送」绕过审计一致性。
    不一致抛 AI_IMPORT_REASON_MISMATCH。
    """
    if preview_reason != execute_reason:
        raise BusinessRuleException(
            "execute 阶段 reason 必须与 preview 阶段一致（spec §2.30）",
            error_code="AI_IMPORT_REASON_MISMATCH",
        )


async def cleanup_expired_previews(*args: Any, **kwargs: Any) -> None:
    """PREVIEW_DONE 超 10min → EXPIRED，删孤儿 preview 文件。

    Task 22 落地：每小时 cron，spec §2.26 + v2.2 P1-2。
    """


async def cleanup_expired_export_tasks(*args: Any, **kwargs: Any) -> None:
    """清理 30 天前 ExportTask + 关联 export 文件。

    Task 22 落地：每日 02:30 cron，spec §2.31。
    """
