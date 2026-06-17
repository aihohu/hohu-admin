from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base


class AppRating(Base):
    """应用市场 - 用户评分 (spec 14.6)。"""

    __tablename__ = "mk_app_rating"
    __table_args__ = (
        UniqueConstraint("app_id", "user_id", name="uq_mk_app_rating_app_user"),
        Index("ix_mk_app_rating_app", "app_id"),
        CheckConstraint(
            "rating BETWEEN 1 AND 5",
            name="ck_mk_app_rating_range",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="评分ID"
    )
    app_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("mk_app.id", ondelete="CASCADE"),
        nullable=False,
        comment="被评分应用ID",
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sys_user.user_id", ondelete="CASCADE"),
        nullable=False,
        comment="评分用户ID",
    )
    rating: Mapped[int] = mapped_column(
        SmallInteger,
        nullable=False,
        comment="评分（1-5）",
    )
    comment: Mapped[str | None] = mapped_column(Text, nullable=True, comment="评分评论")
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
