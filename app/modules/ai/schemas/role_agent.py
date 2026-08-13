"""Role-Agent binding schemas。

统一约定：
- `alias_generator=to_camel` + `from_attributes=True`
- Snowflake ID 声明为 `int` + `@field_serializer` 返回 `str(v)`，
  防止 JavaScript BigInt 精度丢失
"""

from pydantic import BaseModel, ConfigDict, field_serializer
from pydantic.alias_generators import to_camel


class AgentRow(BaseModel):
    """GET /ai/role-agent/{roleId} 响应内 allAgents 的单个 Agent 行.

    决策 #19：不暴露软禁用态，故无 enabled_role_level / softDisabled 字段.
    is_shared: 决策 #14 — shared Agent 直通，前端 UI 显示「无需绑定」徽标.
    """

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, from_attributes=True
    )

    agent_id: int

    @field_serializer("agent_id")
    def _serialize_agent_id(self, v: int) -> str:
        return str(v)

    code: str
    name: str
    description: str
    enabled: bool
    is_builtin: bool
    is_shared: bool


class RoleAgentBinding(BaseModel):
    """GET /ai/role-agent/{roleId} 响应."""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, from_attributes=True
    )

    role_id: int

    @field_serializer("role_id")
    def _serialize_role_id(self, v: int) -> str:
        return str(v)

    all_agents: list[AgentRow]
    bound_agent_ids: list[str]


class RoleAgentBindReq(BaseModel):
    """PUT /ai/role-agent/{roleId} 全量覆盖请求.

    决策 #15：全量覆盖（DELETE + INSERT）而非增量更新 —— 前端只发最终态列表，
    语义简单可重试. agent_ids 为 str（前端序列化的 Snowflake 字符串），
    Service 层 `int(aid)` 还原.
    """

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, from_attributes=True
    )

    agent_ids: list[str]
