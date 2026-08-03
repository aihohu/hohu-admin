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


# 行数硬上限（spec §2.6 / §2.10 / §3.2）
USER_IMPORT_MAX_ROWS = 2000
"""单次导入同步上限。> 2000 拒绝（AI_IMPORT_TOO_MANY_ROWS），引导分批或等 Phase 3 异步通道。"""

USER_IMPORT_SYNC_THRESHOLD = 2000
"""Phase 3 异步切换逻辑用同义词（spec §2.6 双阈值）。当前恒等于 USER_IMPORT_MAX_ROWS。"""

USER_EXPORT_ASYNC_THRESHOLD = 5000
"""单次导出同步上限。> 5000 抛 AI_EXPORT_ASYNC_REQUIRED（Phase 3 异步）。"""

MAX_PREVIEW_RECORDS = 2000
"""预检结果展示上限（spec §3.2）。与 USER_IMPORT_MAX_ROWS 对齐；超出的 records 写 *_records_file。"""


# 字段白名单（spec §2.21 / §3.8 line 1786-1807）
OVERWRITE_NEVER: frozenset[str] = frozenset(
    {
        "user_id",
        "user_name",
        "hashed_password",
        "create_time",
    }
)
"""on_conflict=overwrite 时永不覆盖的字段（spec §2.21）。

- user_id：主键，身份锚点
- user_name：登录账号，改了破坏审计 + 外部系统关联
- hashed_password：默认密码覆盖已改密码 → 用户已改密码失效（安全 + 体验灾难）
- create_time：审计时间戳
"""

OVERWRITE_ALLOWED: frozenset[str] = frozenset(
    {
        "employee_no",  # spec §2.24 v2.2 P1
        "nickname",
        "user_email",
        "user_phone",
        "dept_id",
        "role_ids",
        "user_gender",
        "status",
    }
)
"""on_conflict=overwrite 时允许更新的字段（spec §2.21 / §3.8）。

不含 user_id / user_name / hashed_password（在 OVERWRITE_NEVER 中）。
AI tool / HTTP / 前端统一使用本集合：Excel 中的 user_name 列仅用于"识别已存在"，
不一致 → 跳过/报错，不强行覆盖 user_name（spec §2.21 line 701）。
"""

EXPORT_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        "user_name",
        "nickname",
        "user_email",
        "user_phone",
        "dept_id",
        "role_codes",
        "user_gender",
        "status",
        "create_time",
    }
)
"""导出 Excel 字段白名单（spec §2.9 / §3.6 line 266）。

未列入的字段不进 Excel。新增敏感字段时本白名单不变（默认安全）。
- hashed_password：永不导出（spec §2.9 反例 1）
- employee_no：v2.2 P1 决定不导出（员工工号属 PII，按最小化原则）"""
