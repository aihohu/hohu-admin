"""[CLOUD-ONLY] 审核队列 mk_app_review

部署在云市场 DB。本地 HoHu 不审核，直接信任云上 published 应用。
Phase 2 拆分时迁移到 app/modules/marketplace/models/cloud/review.py
详见 docs/MARKETPLACE-CLOUD-SPLIT.md
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base


class AppReview(Base):
    """应用市场 - 审核记录 (spec 14.3)：规则检查 + AI 风险评估 + 人工审核 三阶段"""

    __tablename__ = "mk_app_review"
    __table_args__ = (Index("ix_mk_app_review_app_version", "app_id", "version_id"),)

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="审核记录ID"
    )
    app_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("mk_app.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属应用ID",
    )
    version_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("mk_app_version.id", ondelete="CASCADE"),
        nullable=False,
        comment="被审核版本ID",
    )
    rule_check_result: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="规则引擎检查结果"
    )
    rule_check_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="规则检查完成时间"
    )
    ai_risk_level: Mapped[str | None] = mapped_column(
        String(10), nullable=True, comment="AI 风险等级: low|medium|high"
    )
    ai_report: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="AI 审核报告"
    )
    ai_review_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="AI 审核完成时间"
    )
    human_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        server_default="pending",
        comment="人工审核状态: pending|approved|rejected",
    )
    human_reviewer_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("sys_user.user_id", ondelete="SET NULL"),
        nullable=True,
        comment="人工审核人ID",
    )
    human_comment: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="人工审核意见"
    )
    human_reviewed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="人工审核完成时间"
    )
    final_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        server_default="pending",
        comment="最终审核状态: pending|approved|rejected",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        comment="更新时间",
    )
