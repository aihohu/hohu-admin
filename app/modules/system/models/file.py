from datetime import datetime

from sqlalchemy import (
    CHAR,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base


class File(Base):
    """文件上传记录模型"""

    __tablename__ = "sys_file"
    __table_args__ = (
        UniqueConstraint("tenant_id", "file_id", name="uq_sys_file_tenant_file_id"),
        ForeignKeyConstraint(
            ("tenant_id", "owner_user_id"),
            ("sys_user.tenant_id", "sys_user.user_id"),
            name="fk_sys_file_tenant_owner",
            ondelete="RESTRICT",
        ),
        Index("ix_sys_file_tenant_owner", "tenant_id", "owner_user_id"),
        Index("ix_sys_file_tenant_deleted", "tenant_id", "del_flag"),
        Index("ix_sys_file_delivery", "tenant_id", "delivery_class", "del_flag"),
        Index("ix_sys_file_public_url", "file_url", "delivery_class"),
        CheckConstraint(
            "(delivery_class = 'legacy-unknown' AND content_sha256 IS NULL AND source_manifest IS NULL) OR "
            "(delivery_class IN ('owned-private','public-image','ai-chat-private','ai-chat-image','user-import') "
            "AND content_sha256 IS NOT NULL AND content_sha256 ~ '^[0-9a-f]{64}$' "
            "AND source_manifest IS NOT NULL AND jsonb_typeof(source_manifest) = 'object')",
            name="ck_sys_file_delivery_proof",
        ),
    )

    file_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="文件ID"
    )
    delivery_class: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="legacy-unknown"
    )
    content_sha256: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    source_manifest: Mapped[dict | None] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    original_name: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="原始文件名"
    )
    file_name: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="存储文件名(Snowflake ID)"
    )
    file_path: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="相对路径"
    )
    file_url: Mapped[str] = mapped_column(
        String(500), nullable=False, comment="文件访问URL"
    )
    file_size: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="文件大小(字节)"
    )
    file_ext: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="文件扩展名"
    )
    mime_type: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="MIME类型"
    )
    business_type: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="业务类型(如product、avatar)"
    )
    business_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="业务记录ID"
    )
    owner_user_id: Mapped[int | None] = mapped_column(
        BigInteger,
        nullable=True,
        comment="文件所有者用户ID（NULL 仅兼容无法回填的历史记录）",
    )
    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sys_tenant.tenant_id", ondelete="RESTRICT"),
        nullable=False,
        comment="租户ID；必须由可信 TenantContext 显式写入",
    )
    del_flag: Mapped[str] = mapped_column(
        String(1), default="0", comment="删除标记: 0-正常, 1-已删除"
    )
    create_by: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="上传者"
    )
    create_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="上传时间"
    )
