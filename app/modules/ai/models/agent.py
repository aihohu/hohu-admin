from datetime import datetime
from typing import Literal

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.id_generator import next_id
from app.db.base import Base

RiskAppetite = Literal["conservative", "balanced", "aggressive"]


class AiAgent(Base):
    """AI Agent 注册中心

    Agent 是工具集合的分组，与工具实现解耦：
    - 代码层 @ai_tool(agent="user_mgmt", ...) 仅声明归属
    - DB 层 ai_agent 行持有 system_prompt / model_preference / enabled 等运行时配置
    - 两者通过 code 字段强约束（启动时 ToolRegistry 校验）

    ORM 安全默认值为 enabled=False；fresh seed 仅开启当前阶段已完成的 Agent，
    upgrade 永远保留部署方已有 enabled 状态。
    """

    __tablename__ = "ai_agent"

    agent_id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, default=next_id, comment="AgentID"
    )
    code: Mapped[str] = mapped_column(
        String(64),
        unique=True,
        nullable=False,
        comment="Agent code，如 'user_mgmt' / 'shared'，与 @ai_tool(agent=...) 对应",
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, comment="显示名")
    description: Mapped[str] = mapped_column(Text, nullable=False, comment="描述")
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="全局开关；ORM 默认禁用，fresh seed 按阶段发布集合决定",
    )
    is_builtin: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="是否内置 Agent（开源项目自带），UI 不允许删除",
    )
    display_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="排序"
    )
    system_prompt: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        comment="管理员 custom prompt，与固定 SAFETY_PREAMBLE 拼接，应用层限制 32KB",
    )
    model_preference: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="格式 'provider:model'，会话创建时作默认值，None=用全局默认",
    )
    daily_quota_per_user: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        default=None,
        comment="Agent 日配额上限，None 表示仅使用全局 L2 配额",
    )
    risk_appetite: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="balanced",
        comment="风险偏好：conservative（high 永远 HITL）/ "
        "balanced（默认，high + dry_run_count≤1 autonomous）/ "
        "aggressive（high 永远 autonomous）。仅影响 high risk，"
        "destructive / hitl_always / injection_hit 不受影响",
    )
    create_time: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), comment="创建时间"
    )
    update_time: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        comment="更新时间",
    )
