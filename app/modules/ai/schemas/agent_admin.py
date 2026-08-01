"""Multi-Agent admin UI schemas (spec §6.1)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer
from pydantic.alias_generators import to_camel

from app.modules.ai.models.agent import RiskAppetite


class AgentAdminListItem(BaseModel):
    """GET /ai/admin/agents list item（不含 systemPrompt，spec 决策 #5）."""

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
    display_order: int
    model_preference: str | None = None
    daily_quota_per_user: int | None = None
    risk_appetite: RiskAppetite
    create_time: datetime
    update_time: datetime


class AgentAdminDetailItem(AgentAdminListItem):
    """GET /ai/admin/agents/{id} detail（含 systemPrompt）."""

    system_prompt: str


class AgentAdminUpdateReq(BaseModel):
    """PUT /ai/admin/agents/{id} partial update（spec §6.1）.

    校验分层说明：description 长度 + model_preference 格式 + daily_quota_per_user
    取值由 Service 层抛 BusinessRuleException 处理，目的是产出精确 errorCode
    （供前端 i18n 映射）。Pydantic 全局 RequestValidationError handler 返 422 +
    无 errorCode，无法满足契约；故这三个字段不再加 field_validator，避免 Pydantic
    在请求解析阶段提前拦截。name 等无精确 errorCode 需求的约束仍走 Pydantic Field.
    """

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, from_attributes=True
    )

    name: str | None = Field(None, min_length=1, max_length=128)
    description: str | None = None
    enabled: bool | None = None
    display_order: int | None = Field(None, ge=0)
    system_prompt: str | None = Field(None, max_length=32 * 1024)
    model_preference: str | None = Field(None, max_length=128)
    daily_quota_per_user: int | None = None
    risk_appetite: RiskAppetite | None = None
