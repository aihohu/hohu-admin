"""导入状态机、CAS helper 与兼容清理入口。

CAS helper 用 sqlalchemy.Table 抽象写 raw UPDATE；
ORM 的 synchronize_session 容易引入意外），同时避免循环依赖 ORM。
该路径依赖精确 rowcount 判断并发状态是否匹配。
"""

from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    update,
)
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessRuleException, UnprocessableEntityException
from app.modules.system.constants import (
    LEGAL_TRANSITIONS,
    ImportBatchStatus,
)

_batch_metadata = MetaData()
_batch_table = Table(
    "sys_user_import_batch",
    _batch_metadata,
    Column("batch_id", String(64), primary_key=True),
    Column("tenant_id", Integer, nullable=False),
    Column(
        "status",
        postgresql.ENUM(
            "CREATED",
            "PREVIEW_DONE",
            "RUNNING",
            "SUCCESS",
            "PARTIAL_SUCCESS",
            "FAILED",
            "EXPIRED",
            "CANCELLED",
            name="import_batch_status",
            create_type=False,
        ),
        nullable=False,
    ),
    # CAS UPDATE 可同步写这些汇总列。
    Column("summary_new", Integer),
    Column("summary_exists", Integer),
    Column("summary_conflict", Integer),
    Column("summary_out_of_scope", Integer),
    Column("success_count", Integer),
    Column("skipped_count", Integer),
    Column("overwritten_count", Integer),
    Column("failed_count", Integer),
    Column("failed_rows_file", String(512)),
    Column("started_at", DateTime),
    Column("finished_at", DateTime),
    extend_existing=True,
)


def validate_transition(
    from_status: ImportBatchStatus,
    to_status: ImportBatchStatus,
) -> None:
    """校验状态转换合法性，非法转换抛 AI_IMPORT_ILLEGAL_TRANSITION。

    调用方在写 DB 前先校验集中状态机，
    让非法业务逻辑（如终态 → 任意态）在 service 层就被拒绝，避免脏 UPDATE。
    """
    if to_status not in LEGAL_TRANSITIONS.get(from_status, frozenset()):
        raise UnprocessableEntityException(
            f"非法状态转换 {from_status.value} → {to_status.value}",
            error_code="AI_IMPORT_ILLEGAL_TRANSITION",
        )


async def _transition_batch_status(
    db: AsyncSession,
    batch_id: str,
    from_status: ImportBatchStatus,
    to_status: ImportBatchStatus,
    *,
    tenant_id: int,
    **updates: Any,
) -> bool:
    """CAS 状态转换，防止并发覆盖。

    Returns:
        True: 转换成功（rowcount=1）
        False: from_status 不匹配（rowcount=0），调用方按业务语义处理
              （调用方可按业务语义视为幂等成功）

    Notes:
        raw UPDATE 绕过 ORM synchronize_session；调用方在依赖 batch 实例新状态时
        需自行 ``await db.refresh(batch)`` 或重新查询。
    """
    validate_transition(from_status, to_status)
    set_values = {**updates, "status": to_status.value}
    result = await db.execute(
        update(_batch_table)
        .where(
            _batch_table.c.tenant_id == tenant_id,
            _batch_table.c.batch_id == batch_id,
            _batch_table.c.status == from_status.value,
        )
        .values(**set_values)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def validate_reason_consistency(
    preview_reason: str,
    execute_reason: str,
) -> None:
    """校验预览与执行阶段的业务理由一致。

    防止用户 preview 时填「HR 同步」，execute 时填「ERP 推送」绕过审计一致性。
    不一致抛 AI_IMPORT_REASON_MISMATCH。
    """
    if preview_reason != execute_reason:
        raise BusinessRuleException(
            "execute 阶段 reason 必须与 preview 阶段一致",
            error_code="AI_IMPORT_REASON_MISMATCH",
        )
