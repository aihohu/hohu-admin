"""[LOCAL-ONLY] 本地安装记录 mk_tenant_app

部署在本地 DB，云市场不知道用户装了什么。
Phase 2 拆分时迁移到 app/modules/marketplace/models/local/install.py
详见 docs/MARKETPLACE-CLOUD-SPLIT.md
"""

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base


class TenantApp(Base):
    """应用市场 - 租户安装记录 (spec 14.4)。

    注意：status 默认 'installed'（spec 决策 #10），不是 'disabled'。
    """

    __tablename__ = "mk_tenant_app"
    __table_args__ = (
        UniqueConstraint("tenant_id", "app_id", name="uq_mk_tenant_app_tenant_app"),
        Index("ix_mk_tenant_app_tenant_status", "tenant_id", "status"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="安装记录ID"
    )
    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=0,
        server_default="0",
        comment="租户ID（Phase 1 单租户默认 0）",
    )
    app_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("mk_app.id", ondelete="CASCADE"),
        nullable=False,
        comment="已安装应用ID",
    )
    installed_version: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="已安装版本号（semver 字符串）"
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="installed",
        server_default="installed",
        comment="安装状态: installed|disabled|uninstalled",
    )
    config: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="安装配置（用户填写的）"
    )
    approved_permissions: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, nullable=True, comment="用户已批准的权限清单"
    )
    retained_table_names: Mapped[list[str] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="卸载后保留的数据表名清单",
    )
    has_data: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="false",
        comment="是否存在业务数据（决定是否可硬删除）",
    )
    installed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        comment="首次安装时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
        comment="更新时间",
    )
