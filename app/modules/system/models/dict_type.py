from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base


class DictType(Base):
    """字典类型模型"""

    __tablename__ = "sys_dict_type"

    dict_type_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="字典类型ID"
    )
    dict_name: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True, comment="字典名称"
    )
    dict_type: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True, comment="字典类型"
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
