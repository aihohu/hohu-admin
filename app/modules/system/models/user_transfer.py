"""用户导入导出 ORM。

User ORM 仍在 app.modules.system.models.user（不动），
本模块只含导入导出相关 ORM：
- UserImportBatch（sys_user_import_batch，导入聚合根）
- UserImportBatchLog（sys_user_import_batch_log，FK CASCADE）
- UserExportTask（sys_user_export_task）

sys_user.employee_no 字段位于现有 User ORM。
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base
from app.modules.system.constants import (
    ExportTaskStatus,
    ImportBatchStatus,
)


class UserImportBatch(Base):
    """一次导入的批次上下文和状态机。

    状态机：CREATED → PREVIEW_DONE → RUNNING →
            SUCCESS / PARTIAL_SUCCESS / FAILED / EXPIRED / CANCELLED
    """

    __tablename__ = "sys_user_import_batch"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "batch_id", name="uq_sys_user_import_batch_tenant_batch"
        ),
        UniqueConstraint(
            "tenant_id",
            "preview_token",
            name="uq_sys_user_import_batch_tenant_preview_token",
        ),
        ForeignKeyConstraint(
            ("tenant_id", "operator_id"),
            ("sys_user.tenant_id", "sys_user.user_id"),
            name="fk_sys_user_import_batch_tenant_operator",
            ondelete="RESTRICT",
        ),
        Index("ix_sys_user_import_batch_tenant_status", "tenant_id", "status"),
    )

    batch_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="UUID")
    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sys_tenant.tenant_id", ondelete="RESTRICT"),
        nullable=False,
        comment="租户ID；必须由可信 TenantContext 显式写入",
    )
    operator_id: Mapped[int] = mapped_column(BigInteger, index=True)
    filename: Mapped[str] = mapped_column(String(256))
    file_sha256: Mapped[str] = mapped_column(String(64))
    records_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="records 序列化后的 sha256，执行时比对以防预览后字段被修改",
    )
    total_rows: Mapped[int] = mapped_column(Integer)

    preview_token: Mapped[str] = mapped_column(
        String(64),
        comment="执行时用于反查批次并保证幂等",
    )
    summary_new: Mapped[int] = mapped_column(Integer, default=0)
    summary_exists: Mapped[int] = mapped_column(Integer, default=0)
    summary_conflict: Mapped[int] = mapped_column(Integer, default=0)
    summary_out_of_scope: Mapped[int] = mapped_column(Integer, default=0)

    success_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    overwritten_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_rows_file: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="失败行 Excel storage_key"
    )

    file_storage_key: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="原始上传文件的 storage key",
    )

    on_conflict: Mapped[str] = mapped_column(
        String(16), comment="skip / overwrite / fail_fast"
    )
    reason: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="批量操作的业务理由，进入审计链路",
    )

    status: Mapped[ImportBatchStatus] = mapped_column(
        SAEnum(
            ImportBatchStatus,
            name="import_batch_status",
            values_callable=lambda x: [e.value for e in x],
            native_enum=True,
            create_constraint=True,
        ),
        nullable=False,
        index=True,
        default=ImportBatchStatus.CREATED,
        comment="导入批次状态",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UserImportBatchLog(Base):
    """批次操作日志。

    按 batch 维度记录状态转换 + 关键节点（CHUNK_PROGRESS 等）。
    FK ondelete=CASCADE：删 batch 自动删 log。
    """

    __tablename__ = "sys_user_import_batch_log"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "log_id", name="uq_sys_user_import_log_tenant_log"
        ),
        ForeignKeyConstraint(
            ("tenant_id", "batch_id"),
            ("sys_user_import_batch.tenant_id", "sys_user_import_batch.batch_id"),
            name="fk_sys_user_import_log_tenant_batch",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ("tenant_id", "operator_id"),
            ("sys_user.tenant_id", "sys_user.user_id"),
            name="fk_sys_user_import_log_tenant_operator",
            ondelete="RESTRICT",
        ),
        Index("ix_sys_user_import_log_tenant_batch", "tenant_id", "batch_id"),
    )

    log_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: str(next_id()),
        comment="Snowflake ID",
    )
    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sys_tenant.tenant_id", ondelete="RESTRICT"),
        nullable=False,
        comment="租户ID；必须由可信 TenantContext 显式写入",
    )
    batch_id: Mapped[str] = mapped_column(
        String(64),
        index=True,
    )
    operator_id: Mapped[int] = mapped_column(BigInteger, index=True)
    event: Mapped[str] = mapped_column(
        String(32),
        comment="事件：CREATED/PREVIEW_DONE/EXECUTE_START/CHUNK_PROGRESS/EXECUTE_FINISH/EXECUTE_FAILED/EXPIRED/CANCELLED",
    )
    from_status: Mapped[ImportBatchStatus | None] = mapped_column(
        SAEnum(
            ImportBatchStatus,
            name="import_batch_status",
            values_callable=lambda x: [e.value for e in x],
            native_enum=True,
            create_constraint=False,
            create_type=False,
        ),
        nullable=True,
    )
    to_status: Mapped[ImportBatchStatus | None] = mapped_column(
        SAEnum(
            ImportBatchStatus,
            name="import_batch_status",
            values_callable=lambda x: [e.value for e in x],
            native_enum=True,
            create_constraint=False,
            create_type=False,
        ),
        nullable=True,
    )
    detail: Mapped[dict] = mapped_column(
        JSON,
        comment="事件详情：chunk_index / chunk_size / failed_in_chunk / error_message / reason 等",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )


class UserExportTask(Base):
    """用户导出任务审计。

    所有导出（HTTP 同步 / HTTP 异步 / AI）一律建任务记录，
    高风险数据外流动作可追溯。
    """

    __tablename__ = "sys_user_export_task"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "export_id", name="uq_sys_user_export_tenant_export"
        ),
        ForeignKeyConstraint(
            ("tenant_id", "operator_id"),
            ("sys_user.tenant_id", "sys_user.user_id"),
            name="fk_sys_user_export_tenant_operator",
            ondelete="RESTRICT",
        ),
        Index("ix_sys_user_export_tenant_status", "tenant_id", "status"),
    )

    export_id: Mapped[str] = mapped_column(
        String(64),
        primary_key=True,
        default=lambda: str(next_id()),
        comment="Snowflake ID",
    )
    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sys_tenant.tenant_id", ondelete="RESTRICT"),
        nullable=False,
        comment="租户ID；必须由可信 TenantContext 显式写入",
    )
    operator_id: Mapped[int] = mapped_column(BigInteger, index=True)
    filter_snapshot: Mapped[dict] = mapped_column(
        JSON,
        comment="filter 快照（含 accessible_dept_ids 解析后的部门 ID 集合），防事后改 filter 反查时漂移",
    )
    reason: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="导出的业务理由，与导入批次理由语义一致",
    )

    row_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    file_storage_key: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
        comment="导出文件 storage_key（FileStorage Protocol）",
    )
    file_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)

    status: Mapped[ExportTaskStatus] = mapped_column(
        SAEnum(
            ExportTaskStatus,
            name="export_task_status",
            values_callable=lambda x: [e.value for e in x],
            native_enum=True,
            create_constraint=True,
        ),
        nullable=False,
        index=True,
        default=ExportTaskStatus.CREATED,
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
