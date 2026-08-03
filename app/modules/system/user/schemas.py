"""用户导入导出 Pydantic schemas（spec §3 + §2.30，Task 1 + Task 0e）。

Pydantic v2 惯例（与 app/modules/system/schemas/user.py 对齐）：
- alias_generator=to_camel + populate_by_name=True
- BigInteger ID 通过 @field_serializer 序列化为字符串（防 JS BigInt 精度丢失）
- 时间字段 ISO 8601 字符串

ORM（UserImportBatch / UserExportTask）在 models.py Task 2 落地，
本模块仅含 API 响应 / 请求 body 的 Pydantic 类 + ReasonSchema mixin。
"""

from datetime import datetime
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
)
from pydantic.alias_generators import to_camel


class _CamelBase(BaseModel):
    """项目内 Pydantic v2 基类（snake_case ↔ camelCase 自动转换）。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ReasonSchema(BaseModel):
    """批量操作审计理由 mixin（spec §2.30 v2.2 P1-3）。

    所有批量写入操作（import preview / execute / cancel / export）必填 reason，
    进入 sys_user_import_batch.reason + batch_log.detail.reason 审计链路。

    使用方式（多继承）：
        class ImportPreviewRequest(ReasonSchema):
            file: UploadFile
            ...
    """

    reason: str = Field(
        ...,
        min_length=1,
        max_length=256,
        description="业务理由（1-256 字符），写审计链路。例：「2026年8月 HR 入职名单同步」",
    )

    @field_validator("reason")
    @classmethod
    def _strip_and_require_non_empty(cls, v: str) -> str:
        """strip 后存库（防审计字段两端空白漂移）；全空白拒绝（spec §2.30 line 1420）。"""
        stripped = v.strip()
        if not stripped:
            raise ValueError("reason 不能为空或全空白")
        return stripped


class UserImportRecord(_CamelBase):
    """Excel 一行 → 一个 record（spec §3.1）。"""

    row_num: int = Field(..., description="Excel 行号（错误定位用）")
    user_name: str = Field(..., min_length=2, max_length=16, description="账号（必填）")
    employee_no: str | None = Field(
        None, max_length=64, description="员工工号（spec §2.24）"
    )
    nickname: str | None = Field(None, max_length=16)
    user_email: str | None = Field(None, max_length=128)
    user_phone: str | None = Field(None, max_length=32)
    dept_input: str = Field(
        ..., description="部门名 or 完整路径，如「前端部」/「总公司/研发中心/前端部」"
    )
    role_input: str | None = Field(
        None,
        description="逗号分隔 code/name 混合，如「R_ADMIN,内容编辑」",
    )
    user_gender: Literal["0", "1", "2"] = Field("0", description="0 未知 / 1 男 / 2 女")
    status: Literal["0", "1"] = Field("1", description="0 禁用 / 1 启用（默认启用）")


class FailedRow(_CamelBase):
    """导入失败行（spec §3.4）。"""

    row_num: int
    field: str = Field(..., description="出错字段名")
    value: str = Field(..., description="用户填的值")
    reason: str = Field(..., description="中文原因（前端展示）")
    error_code: str = Field(
        ...,
        description="i18n 映射码，如 AI_IMPORT_USERNAME_INVALID（前端 $t('errorCode.XXX') 映射）",
    )


class ImportDryRunResult(_CamelBase):
    """预检结果（spec §3.2 v2.2 P1：records 限流 + 大批走文件下载）。

    records 截断到 MAX_PREVIEW_RECORDS=2000，超出写 *_records_file 供下载。
    """

    total: int = Field(..., description="Excel 总行数")
    new_records: list[UserImportRecord] = Field(
        default_factory=list, description="截断到 MAX_PREVIEW_RECORDS（前端展示）"
    )
    exists_records: list[UserImportRecord] = Field(default_factory=list)
    conflict_records: list[FailedRow] = Field(default_factory=list)
    out_of_scope_records: list[FailedRow] = Field(default_factory=list)
    new_records_truncated: bool = False
    exists_records_truncated: bool = False
    conflict_records_truncated: bool = False
    out_of_scope_records_truncated: bool = False
    conflict_records_file: str | None = Field(
        None, description="/file/import-preview/{batch_id}/conflicts.xlsx"
    )
    out_of_scope_records_file: str | None = None

    @property
    def new_count(self) -> int:
        """截断后长度（前端展示用）。"""
        return len(self.new_records)

    @property
    def exists_count(self) -> int:
        return len(self.exists_records)

    @property
    def conflict_count(self) -> int:
        return len(self.conflict_records)

    @property
    def out_of_scope_count(self) -> int:
        return len(self.out_of_scope_records)


class ImportResult(_CamelBase):
    """正式导入结果（spec §3.3 v2.2 P1：API 不返回 failed_rows 数组，只返文件）。"""

    batch_id: str = Field(
        ..., description="关联 sys_user_import_batch（spec §2.27 幂等用）"
    )
    success_count: int
    skipped_count: int = Field(0, description="on_conflict=skip 命中的")
    overwritten_count: int = Field(0, description="on_conflict=overwrite 命中的")
    failed_count: int = Field(..., description="失败行数（仅计数，不返数组）")
    failed_rows_file: str | None = Field(
        None, description="/file/import-error/{batch_id}.xlsx（下载链接）"
    )
    failed_rows_preview: list[FailedRow] = Field(
        default_factory=list,
        description="前 N 条失败摘要（前端 toast 显示用，全量在文件）",
    )
    idempotent_replay: bool = Field(
        False,
        description="True 表示这是幂等重放，非首次执行（spec §2.27）",
    )


class UserExportFilter(_CamelBase):
    """导出筛选（spec §3.5，POST body）。"""

    user_name: str | None = None
    nickname: str | None = None
    user_email: str | None = None
    user_phone: str | None = None
    dept_id: str | None = Field(None, description="部门 ID（Snowflake 字符串）")
    status: Literal["0", "1"] | None = None


class UserImportBatchResponse(_CamelBase):
    """导入批次 API 响应（spec §3.6 v2.2 P1-2：唯一 aggregate root）。

    Pydantic 类，与 ORM UserImportBatch（models.py Task 2）分离：
    - ORM 用于 DB 层
    - 本响应类用于 API 序列化（ID 字符串化、时间 ISO 字符串）
    """

    batch_id: str
    operator_id: int
    filename: str
    file_sha256: str
    total_rows: int
    preview_token: str
    summary_new: int = 0
    summary_exists: int = 0
    summary_conflict: int = 0
    summary_out_of_scope: int = 0
    success_count: int = 0
    skipped_count: int = 0
    overwritten_count: int = 0
    failed_count: int = 0
    failed_rows_file: str | None = None
    on_conflict: Literal["skip", "overwrite", "fail_fast"]
    status: str
    reason: str
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @field_serializer("operator_id")
    def _serialize_operator_id(self, v: int) -> str:
        """Snowflake ID 字符串化（防 JS BigInt 精度丢失）。"""
        return str(v)


class UserExportTaskResponse(_CamelBase):
    """导出任务 API 响应（spec §2.31 v2.2 P1-5）。"""

    export_id: str
    operator_id: int
    filter_snapshot: dict
    reason: str
    row_count: int | None = None
    file_storage_key: str | None = None
    file_size_bytes: int | None = None
    status: str
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    duration_ms: int | None = None

    @field_serializer("operator_id")
    def _serialize_operator_id(self, v: int) -> str:
        return str(v)


__all__ = [
    "FailedRow",
    "ImportDryRunResult",
    "ImportResult",
    "ReasonSchema",
    "UserExportFilter",
    "UserExportTaskResponse",
    "UserImportBatchResponse",
    "UserImportRecord",
]
