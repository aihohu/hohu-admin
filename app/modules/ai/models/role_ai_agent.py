from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    PrimaryKeyConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RoleAiAgent(Base):
    """角色 ↔ Agent RBAC 关联表

    与 sys_role_menu 使用同构的角色绑定模式：
    - 角色 R 绑定 Agent A → 该角色的用户能用 A（前提 A.enabled=True）
    - 超管与 shared Agent 均无旁路，必须显式绑定
    - role_id ↔ agent_id 联合主键

    enabled 字段：role 级"软禁用"，比直接删行更友好（保留绑定关系，临时关闭）。
    """

    __tablename__ = "role_ai_agent"
    __table_args__ = (
        PrimaryKeyConstraint("tenant_id", "role_id", "agent_id"),
        ForeignKeyConstraint(
            ("tenant_id", "role_id"),
            ("sys_role.tenant_id", "sys_role.role_id"),
            name="fk_role_ai_agent_tenant_role",
            ondelete="CASCADE",
        ),
        Index("ix_role_ai_agent_tenant_agent", "tenant_id", "agent_id"),
    )

    tenant_id: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        comment="租户ID；Agent 本身保持 platform-global",
    )

    role_id: Mapped[int] = mapped_column(
        BigInteger,
        comment="角色ID",
    )
    agent_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("ai_agent.agent_id", ondelete="CASCADE"),
        comment="AgentID",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        comment="role 级软禁用，false=该角色用户看不到此 Agent",
    )
    create_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间"
    )
