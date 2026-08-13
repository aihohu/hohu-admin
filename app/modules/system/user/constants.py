"""用户导入导出模块常量。

故意不进 settings，避免部署方误改导致安全边界被绕过。
"""

import enum


class ImportBatchStatus(enum.StrEnum):
    """导入批次状态机。

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
    """employee_no 命中已有用户时的同步策略。

    CREATE_ONLY 默认最安全；UPDATE_PROFILE / FULL_SYNC 用于 HR 月度同步。
    """

    CREATE_ONLY = "CREATE_ONLY"
    UPDATE_PROFILE = "UPDATE_PROFILE"
    FULL_SYNC = "FULL_SYNC"


class ExportTaskStatus(enum.StrEnum):
    """导出任务状态机。"""

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
"""合法状态转换映射。

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


# 单次导入行数硬上限。
USER_IMPORT_MAX_ROWS = 2000
"""单次导入同步上限。> 2000 稳定拒绝（AI_IMPORT_TOO_MANY_ROWS）并引导分批。"""

USER_IMPORT_SYNC_THRESHOLD = 2000
"""兼容既有命名；当前仅表示同步硬上限，不承担自动切换执行模式的语义。"""

USER_EXPORT_ASYNC_THRESHOLD = 5000
"""兼容既有命名；> 5000 稳定拒绝，不自动入队。"""

MAX_PREVIEW_RECORDS = 2000
"""预检结果展示上限；超出的记录写入结果文件。"""


# 导入覆盖字段白名单。
OVERWRITE_NEVER: frozenset[str] = frozenset(
    {
        "user_id",
        "user_name",
        "hashed_password",
        "create_time",
    }
)
"""on_conflict=overwrite 时永不覆盖的字段。

- user_id：主键，身份锚点
- user_name：登录账号，改了破坏审计 + 外部系统关联
- hashed_password：默认密码覆盖已改密码 → 用户已改密码失效（安全 + 体验灾难）
- create_time：审计时间戳
"""

OVERWRITE_ALLOWED: frozenset[str] = frozenset(
    {
        "employee_no",
        "nickname",
        "user_email",
        "user_phone",
        "dept_id",
        "role_ids",
        "user_gender",
        "status",
    }
)
"""on_conflict=overwrite 时允许更新的字段。

不含 user_id / user_name / hashed_password（在 OVERWRITE_NEVER 中）。
AI tool / HTTP / 前端统一使用本集合：Excel 中的 user_name 列仅用于"识别已存在"，
不一致时跳过或报错，不强行覆盖 user_name。
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
"""导出 Excel 字段白名单。

未列入的字段不进 Excel。新增敏感字段时本白名单不变（默认安全）。
- hashed_password：永不导出
- employee_no：属于 PII，按最小化原则不导出"""


# 分块提交大小与可恢复错误码白名单。
USER_IMPORT_CHUNK_SIZE = 100
"""每 100 行提交一次，控制 undo segment 和锁持有时间。"""

RECOVERABLE_ERROR_CODES: frozenset[str] = frozenset(
    {
        # 业务校验类（单行问题，不影响其他行）
        "AI_IMPORT_USERNAME_DUPLICATE",
        "AI_IMPORT_EMPLOYEE_NO_DUPLICATE",  # employee_no 并发冲突
        "AI_IMPORT_EMPLOYEE_NO_EXISTS",  # CREATE_ONLY 模式拒绝已存在 employee_no
        "AI_IMPORT_DEPT_NOT_FOUND",
        "AI_IMPORT_DEPT_PATH_NOT_FOUND",
        "AI_IMPORT_DEPT_DUPLICATE",
        "AI_IMPORT_ROLE_NOT_FOUND",
        "AI_IMPORT_DEPT_OUT_OF_SCOPE",  # data_scope 越界
        "AI_IMPORT_ROLE_OUT_OF_SCOPE",
        "AI_IMPORT_USERNAME_INVALID",  # 字段格式
        "AI_IMPORT_EMAIL_INVALID",
        "AI_IMPORT_PHONE_INVALID",
        "VALIDATION_ERROR",  # Pydantic 通用校验失败
        "BusinessRuleException",  # 业务规则（catch-all 业务异常）
    }
)
"""可恢复错误码白名单。

不在此白名单的异常视为致命错误 → chunk transaction rollback + abort 整批
（OperationalError / InterfaceError / MemoryError / TimeoutError 等直接冒泡）。
"""

FAILED_ROWS_PREVIEW_LIMIT = 20
"""API 响应 failed_rows_preview 上限。

2000 行 Excel 全失败的 response 不应撑到几 MB；前 20 条给前端 toast，
全量走 failed_rows_file Excel 下载链接。
"""
