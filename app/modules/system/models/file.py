from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base


class File(Base):
    """文件上传记录模型"""

    __tablename__ = "sys_file"

    file_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="文件ID"
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
        nullable=False,
        default=0,
        server_default="0",
        comment="租户ID（当前单租户固定为0）",
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
