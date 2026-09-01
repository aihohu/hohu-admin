from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base


class DictData(Base):
    """字典数据模型"""

    __tablename__ = "sys_dict_data"
    __table_args__ = (
        UniqueConstraint("tenant_id", "dict_code", name="uq_sys_dict_data_tenant_code"),
        ForeignKeyConstraint(
            ("tenant_id", "dict_type"),
            ("sys_dict_type.tenant_id", "sys_dict_type.dict_type"),
            name="fk_sys_dict_data_tenant_type",
            ondelete="RESTRICT",
        ),
        Index("ix_sys_dict_data_tenant_type", "tenant_id", "dict_type"),
    )

    dict_code: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="字典编码"
    )
    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sys_tenant.tenant_id", ondelete="RESTRICT"),
        nullable=False,
        comment="租户ID；必须由可信 TenantContext 显式写入",
    )
    dict_sort: Mapped[int] = mapped_column(nullable=False, comment="字典排序")
    dict_label: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="字典标签"
    )
    dict_value: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="字典键值"
    )
    dict_type: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="字典类型"
    )
    css_class: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="样式属性"
    )
    list_class: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="表格回显样式"
    )
    is_default: Mapped[str] = mapped_column(
        String(2), nullable=False, default="N", comment="是否默认：Y-是，N-否"
    )
    status: Mapped[str] = mapped_column(
        String(2), nullable=False, default="1", comment="状态：1-启用，2-禁用"
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
