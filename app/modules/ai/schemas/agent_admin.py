"""Multi-Agent admin UI schemas (spec §6.1)."""

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator
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
    """PUT /ai/admin/agents/{id} partial update（spec §6.1）."""

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

    @field_validator("description")
    @classmethod
    def _validate_desc_length(cls, v: str | None) -> str | None:
        # partial update：None 表示未传，跳过校验（spec 决策 #20）
        if v is None:
            return None
        if not (50 <= len(v) <= 200):
            raise ValueError("description 长度必须在 50-200 字之间")
        return v

    @field_validator("daily_quota_per_user")
    @classmethod
    def _validate_quota(cls, v: int | None) -> int | None:
        if v is not None and v <= 0:
            raise ValueError("daily_quota_per_user 必须 ≥ 1 或 null")
        return v

    @field_validator("model_preference")
    @classmethod
    def _validate_model_pref(cls, v: str | None) -> str | None:
        if v is None:
            return None

        if not re.match(r"^[a-z0-9_-]+:[a-z0-9_-]+$", v):
            raise ValueError("model_preference 必须为 'provider:model' 格式")
        return v
