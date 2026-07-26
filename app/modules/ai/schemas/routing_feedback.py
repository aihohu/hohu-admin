"""spec §6.4: routing feedback request schema."""

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
