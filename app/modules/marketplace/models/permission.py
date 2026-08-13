"""[CLOUD-ONLY] 应用权限声明 mk_app_permission

部署在云市场 DB（应用声明的资源，不是用户授权）。
如按云端与本地职责拆分，本模型归入 cloud/permission.py。
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
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base


class AppPermission(Base):
    """应用市场 - 应用声明的权限 (spec 14.5)。

    detail_hash: detail 的 canonical JSON SHA-256（见 app.utils.permission_hash）。
    detail_canonical: 审计字段，便于 hash 算法迁移/复算。
    """

    __tablename__ = "mk_app_permission"
    __table_args__ = (
        UniqueConstraint(
            "app_id", "type", "detail_hash", name="uq_mk_app_permission_app_type_hash"
        ),
        Index("ix_mk_app_permission_app_type", "app_id", "type"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="权限ID"
    )
    app_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("mk_app.id", ondelete="CASCADE"),
        nullable=False,
        comment="所属应用ID",
    )
    type: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        comment="权限类型: api|external_api|menu|db_table|...",
    )
    detail: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, comment="权限详情（原始结构）"
    )
    detail_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="detail canonical JSON 的 SHA-256"
    )
    detail_canonical: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="审计字段：canonical JSON 文本，便于 hash 算法迁移",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="创建时间",
    )
