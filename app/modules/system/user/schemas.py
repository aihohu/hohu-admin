"""用户导入导出 Pydantic schemas（v2.2 P0/P1）。

Task 0e：仅落地 ReasonSchema mixin（spec §2.30 v2.2 P1-3）。
其余 schemas 在 Task 1 落地（UserImportRecord / ImportDryRunResult / ImportResult
/ FailedRow / UserImportBatch / UserExportTask）。
"""

from pydantic import BaseModel, Field, field_validator


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
