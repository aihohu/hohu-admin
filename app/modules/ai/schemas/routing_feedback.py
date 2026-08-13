"""Routing feedback 请求和查询 schema。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator
from pydantic.alias_generators import to_camel


class RoutingFeedbackRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    feedback: str = Field(..., description="'correct' 或 'wrong'")
    corrected_agent_code: str | None = Field(
        None, alias="correctedAgentCode", description="feedback='wrong' 时必填"
    )

    @model_validator(mode="after")
    def _check_correction(self):
        if self.feedback not in ("correct", "wrong"):
            raise ValueError("feedback 必须是 'correct' 或 'wrong'")
        if self.feedback == "wrong" and not self.corrected_agent_code:
            raise ValueError("feedback='wrong' 时必须提供 correctedAgentCode")
        return self


class FeedbackListQuery(BaseModel):
    """GET /ai/routing-feedback/list 查询参数.

    决策 #6：默认 feedback=wrong；不支持 feedback=correct 单独过滤（correct 在
    summary 段聚合成 correct 计数，但不作为列表过滤维度）.
    """

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, from_attributes=True
    )

    days: int = Field(7, ge=1, le=365)
    current: int = Field(1, ge=1)
    size: int = Field(20, ge=1, le=100)
    feedback: str = Field("wrong", pattern="^(wrong|all)$")
    original_agent: str | None = None
    corrected_agent: str | None = None


class TopCorrected(BaseModel):
    """被纠正到的目标 agent top 统计项。"""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, from_attributes=True
    )

    code: str
    name: str
    count: int


class TopWrongAgent(BaseModel):
    """路由错误次数 top 的 agent。"""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, from_attributes=True
    )

    agent_code: str
    agent_name: str
    wrong_count: int
    top_corrected: TopCorrected | None = None


class FeedbackSummary(BaseModel):
    """路由反馈汇总统计。"""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, from_attributes=True
    )

    days: int
    total: int
    correct: int
    wrong: int
    wrong_rate: float
    top_wrong_agents: list[TopWrongAgent]


class FeedbackListItem(BaseModel):
    """路由反馈列表项。"""

    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, from_attributes=True
    )

    feedback_id: int
    message_id: int
    user_id: int
    user_name: str
    original_agent: str
    original_agent_name: str
    feedback: str
    corrected_agent: str | None = None
    corrected_agent_name: str | None = None
    trace_id: str | None = None
    create_time: datetime

    @field_serializer("feedback_id", "message_id", "user_id")
    def _serialize_ids(self, v: int) -> str:
        return str(v)
