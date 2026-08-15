"""HITL resume 的最小状态响应。"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class ResumeStatusOut(BaseModel):
    """失去执行/完整投影权限时仅暴露的固定字段。"""

    confirmation_id: str
    status: str
    error_code: str | None = None
    finished_at: datetime | None = None

    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
    )
