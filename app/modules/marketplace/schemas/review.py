"""审核 Schema（CLOUD-ONLY）。"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_serializer
from pydantic.alias_generators import to_camel

from app.core.config import settings


class _ReviewBase(BaseModel):
    """审核 Schema 公共配置（snake_case ↔ camelCase 自动转换）。"""

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class ReviewListItem(_ReviewBase):
    """审核列表项（联表 app + version）"""

    id: int
    app_id: int
    app_name: str
    app_slug: str
    version_id: int
    version: str
    final_status: str
    ai_risk_level: str | None = None
    reviewer_id: int | None = Field(default=None, alias="humanReviewerId")
    created_at: datetime
    human_reviewed_at: datetime | None = None

    @field_serializer("id", "app_id", "version_id", "reviewer_id")
    def serialize_id(self, v: int | None) -> str | None:
        return str(v) if v is not None else None

    @field_serializer("created_at", "human_reviewed_at")
    def serialize_datetime(self, v: datetime | None) -> str | None:
        return v.strftime(settings.DATETIME_FORMAT) if v is not None else None


class ReviewDetail(ReviewListItem):
    """审核详情（含 manifest、规则检查结果、AI 报告、审核意见）"""

    manifest: dict[str, Any]
    file_size: int | None = None
    rule_check_result: dict[str, Any] | None = None
    ai_report: dict[str, Any] | None = None
    human_comment: str | None = None
    changelog: str | None = None


class ReviewQuery(BaseModel):
    """审核列表查询参数。"""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    current: int = 1
    size: int = 10
    status: str = "pending"  # pending | approved | rejected | all
    app_slug: str | None = None
