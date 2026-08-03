"""用户导入导出 ORM（spec §2.26 / §2.28 / §2.31，Task 2）。

User ORM 仍在 app.modules.system.models.user（不动），
本模块只含导入导出相关 ORM：
- UserImportBatch（sys_user_import_batch，唯一 aggregate root，v2.2 P1-2）
- UserImportBatchLog（sys_user_import_batch_log，FK CASCADE，v2.2 P1 #2.28）
- UserExportTask（sys_user_export_task，v2.2 P1-5 #2.31）

sys_user.employee_no 字段加在现有 User ORM（spec §2.24）。
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base
from app.modules.system.user.constants import (
    ExportTaskStatus,
    ImportBatchStatus,
)


class UserImportBatch(Base):
    """一次导入的批次上下文 + 状态机（spec §3.6 v2.2 P1-2）。

    状态机：CREATED → PREVIEW_DONE → RUNNING →
            SUCCESS / PARTIAL_SUCCESS / FAILED / EXPIRED / CANCELLED
    """

    __tablename__ = "sys_user_import_batch"

    batch_id: Mapped[str] = mapped_column(String(64), primary_key=True, comment="UUID")
    operator_id: Mapped[int] = mapped_column(BigInteger, index=True)
    filename: Mapped[str] = mapped_column(String(256))
    file_sha256: Mapped[str] = mapped_column(String(64))
    total_rows: Mapped[int] = mapped_column(Integer)

    preview_token: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        index=True,
        comment="execute 时反查 batch 行（spec §2.27 幂等）",
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
        comment="原始上传文件 storage_key（spec §3.9 FileStorage Protocol）",
    )

    on_conflict: Mapped[str] = mapped_column(
        String(16), comment="skip / overwrite / fail_fast"
    )
    reason: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="操作理由（spec §2.30 v2.2 P1-3）：批量操作的业务背景，进入审计链路",
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
        comment="状态机详见 §2.26",
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class UserImportBatchLog(Base):
    """批次操作日志（spec §2.28 v2.2 P1）。

    按 batch 维度记录状态转换 + 关键节点（CHUNK_PROGRESS 等）。
    FK ondelete=CASCADE：删 batch 自动删 log。
    """

    __tablename__ = "sys_user_import_batch_log"

    log_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=next_id, comment="Snowflake ID"
    )
    batch_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("sys_user_import_batch.batch_id", ondelete="CASCADE"),
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
    """用户导出任务审计（spec §2.31 v2.2 P1-5）。

    所有导出（HTTP 同步 / HTTP 异步 / AI）一律建任务记录，
    高风险数据外流动作可追溯。
    """

    __tablename__ = "sys_user_export_task"

    export_id: Mapped[str] = mapped_column(
        String(64), primary_key=True, default=next_id, comment="Snowflake ID"
    )
    operator_id: Mapped[int] = mapped_column(BigInteger, index=True)
    filter_snapshot: Mapped[dict] = mapped_column(
        JSON,
        comment="filter 快照（含 accessible_dept_ids 解析后的部门 ID 集合），防事后改 filter 反查时漂移",
    )
    reason: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
        comment="操作理由（spec §2.30）：与 import batch.reason 对称",
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
