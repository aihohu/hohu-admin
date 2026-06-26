"""[CLOUD-ONLY] 应用目录表 mk_app

部署在云市场 DB，本地 HoHu 不创建此表。
Phase 2 拆分时迁移到 app/modules/marketplace/models/cloud/app.py
详见 docs/MARKETPLACE-CLOUD-SPLIT.md
"""

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base


class App(Base):
    """应用市场 - 应用主表 (spec 14.1)"""

    __tablename__ = "mk_app"
    __table_args__ = (
        Index("ix_mk_app_status_category", "status", "category"),
        Index("ix_mk_app_tenant_status", "tenant_id", "status"),
        CheckConstraint(
            "avg_rating >= 0 AND avg_rating <= 5",
            name="ck_mk_app_avg_rating_range",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="应用ID"
    )
    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
        comment="租户ID（Phase 1 单租户默认 0，强制过滤）",
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False, comment="应用名称")
    slug: Mapped[str] = mapped_column(
        String(150), nullable=False, unique=True, comment="URL slug（唯一）"
    )
    type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="应用类型: lowcode|frontend|backend|fullstack|theme|bundle",
    )
    category: Mapped[str] = mapped_column(
        String(30), nullable=False, comment="应用分类"
    )
    description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="应用描述"
    )
    icon: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="图标 URL（对象存储，禁止外链）"
    )
    author_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("sys_user.user_id", ondelete="SET NULL"),
        nullable=True,
        comment="作者用户ID",
    )
    author_name: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="作者名（冗余展示字段）"
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="draft",
        server_default="draft",
        comment="状态: draft|published|...",
    )
    current_version_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("mk_app_version.id", ondelete="SET NULL"),
        nullable=True,
        comment="当前发布版本ID",
    )
    homepage: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="主页 URL"
    )
    license: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="开源协议"
    )
    download_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="累计下载次数",
    )
    avg_rating: Mapped[Decimal] = mapped_column(
        Numeric(3, 1),
        nullable=False,
        default=Decimal("0.0"),
        server_default="0.0",
        comment="平均评分（0-5）",
    )
    rating_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="评分人数",
    )
    tags_text: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="冗余：mk_app_tag 名称以空格拼接，用于 search_vector",
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


class AppVersion(Base):
    """应用市场 - 应用版本表 (spec 14.2)，已去除 review_id（双向 FK 反模式）"""

    __tablename__ = "mk_app_version"
    __table_args__ = (
        UniqueConstraint("app_id", "version", name="uq_mk_app_version_app_version"),
        Index("ix_mk_app_version_app_created", "app_id", "created_at"),
        {"comment": "应用版本（每次发布一行）"},
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="版本ID"
    )
    app_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("mk_app.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属应用ID",
    )
    version: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="语义化版本号（semver）"
    )
    changelog: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="变更说明"
    )
    manifest: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="应用清单（JSON）"
    )
    file_url: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="制品包 URL"
    )
    file_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="制品包 SHA-256"
    )
    file_size: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="制品包大小（字节）"
    )
    review_status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        server_default="pending",
        comment="审核状态: pending|approved|rejected",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="创建时间",
    )
