"""用户模块常量（v2.2 P0/P1）。

故意不进 settings，避免部署方误改导致安全边界被绕过。
"""

import enum


class ImportBatchStatus(enum.StrEnum):
    """导入批次状态机（spec §2.26 + v2.2 P1-2 加 PREVIEW_DONE）。

    DB 层 PostgreSQL ENUM + Python Enum 双重保证（防 typo / 防脏数据）。
    合法转换见 LEGAL_TRANSITIONS，非法转换抛 AI_IMPORT_ILLEGAL_TRANSITION。
    """

    CREATED = "CREATED"
    PREVIEW_DONE = "PREVIEW_DONE"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class EmployeeNoSyncMode(enum.StrEnum):
    """employee_no 冲突时的处理策略（spec §2.24）。

    CREATE_ONLY 默认最安全；UPDATE_PROFILE / FULL_SYNC 用于 HR 月度同步。
    """

    CREATE_ONLY = "CREATE_ONLY"
    UPDATE_PROFILE = "UPDATE_PROFILE"
    FULL_SYNC = "FULL_SYNC"


class ExportTaskStatus(enum.StrEnum):
    """导出任务状态机（spec §2.31）。"""

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"


LEGAL_TRANSITIONS: dict[ImportBatchStatus, frozenset[ImportBatchStatus]] = {
    ImportBatchStatus.CREATED: frozenset(
        {
            ImportBatchStatus.PREVIEW_DONE,
            ImportBatchStatus.FAILED,
        }
    ),
    ImportBatchStatus.PREVIEW_DONE: frozenset(
        {
            ImportBatchStatus.RUNNING,
            ImportBatchStatus.EXPIRED,
            ImportBatchStatus.CANCELLED,
        }
    ),
    ImportBatchStatus.RUNNING: frozenset(
        {
            ImportBatchStatus.SUCCESS,
            ImportBatchStatus.PARTIAL_SUCCESS,
            ImportBatchStatus.FAILED,
        }
    ),
    ImportBatchStatus.SUCCESS: frozenset(),
    ImportBatchStatus.PARTIAL_SUCCESS: frozenset(),
    ImportBatchStatus.FAILED: frozenset(),
    ImportBatchStatus.EXPIRED: frozenset(),
    ImportBatchStatus.CANCELLED: frozenset(),
}
"""合法状态转换映射（spec §2.26）。

终止态映射到空 frozenset，任何向终态的转换由 _transition_batch_status 拒绝。
"""

TERMINAL_STATUSES: frozenset[ImportBatchStatus] = frozenset(
    {
        ImportBatchStatus.SUCCESS,
        ImportBatchStatus.PARTIAL_SUCCESS,
        ImportBatchStatus.FAILED,
        ImportBatchStatus.EXPIRED,
        ImportBatchStatus.CANCELLED,
    }
)
"""终态集合，用于 cleanup cron / cancel 校验。"""
