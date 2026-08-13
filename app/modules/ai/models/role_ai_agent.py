from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RoleAiAgent(Base):
    """角色 ↔ Agent RBAC 关联表

    与 sys_role_menu 使用同构的角色绑定模式：
    - 角色 R 绑定 Agent A → 该角色的用户能用 A（前提 A.enabled=True）
    - 超管 / shared Agent 直通，不需要绑定
    - role_id ↔ agent_id 联合主键

    enabled 字段：role 级"软禁用"，比直接删行更友好（保留绑定关系，临时关闭）。
    """

    __tablename__ = "role_ai_agent"

    role_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("sys_role.role_id"),
        primary_key=True,
        comment="角色ID",
    )
    agent_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("ai_agent.agent_id"),
        primary_key=True,
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
