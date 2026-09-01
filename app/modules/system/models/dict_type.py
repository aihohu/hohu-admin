from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base


class DictType(Base):
    """字典类型模型"""

    __tablename__ = "sys_dict_type"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "dict_type_id", name="uq_sys_dict_type_tenant_type_id"
        ),
        UniqueConstraint("tenant_id", "dict_name", name="uq_sys_dict_type_tenant_name"),
        UniqueConstraint("tenant_id", "dict_type", name="uq_sys_dict_type_tenant_type"),
        Index("ix_sys_dict_type_tenant_status", "tenant_id", "status"),
    )

    dict_type_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="字典类型ID"
    )
    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sys_tenant.tenant_id", ondelete="RESTRICT"),
        nullable=False,
        comment="租户ID；必须由可信 TenantContext 显式写入",
    )
    dict_name: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="字典名称"
    )
    dict_type: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="字典类型"
    )
    status: Mapped[str] = mapped_column(
        String(2), default="1", comment="状态：1-启用，2-禁用"
    )
    remark: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="备注"
    )
    # 审计字段
    create_by: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="创建者"
    )
    create_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间"
    )
    update_by: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="更新者"
    )
    update_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now(), comment="更新时间"
    )
