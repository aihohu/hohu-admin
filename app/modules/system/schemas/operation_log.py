from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer
from pydantic.alias_generators import to_camel

from app.core.config import settings
from app.schemas.types import LocalNaiveDatetime


class OperationLogQuery(BaseModel):
    """操作日志查询参数"""

    current: int = Field(1, ge=1, description="当前页码")
    size: int = Field(10, ge=1, le=100, description="每页数量")
    module: str | None = Field(None, description="业务模块")
    action: str | None = Field(None, description="操作类型")
    username: str | None = Field(None, description="操作人用户名")
    status_code: int | None = Field(None, description="响应状态码")
    start_time: LocalNaiveDatetime | None = Field(
        None, description="操作时间（起），接受 ms timestamp / ISO / datetime"
    )
    end_time: LocalNaiveDatetime | None = Field(
        None, description="操作时间（止），接受 ms timestamp / ISO / datetime"
    )

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class OperationLogOut(BaseModel):
    """操作日志输出"""

    operation_log_id: int
    user_id: int
    username: str
    module: str
    action: str
    method: str
    path: str
    request_params: str | None
    status_code: int | None
    ip: str | None
    duration: int | None
    create_time: datetime

    @field_serializer("operation_log_id", "user_id")
    def serialize_id(self, v: int, _info):
        return str(v)

    @field_serializer("create_time")
    def serialize_create_time(self, dt: datetime) -> str:
        return dt.strftime(settings.DATETIME_FORMAT)

    model_config = ConfigDict(
        from_attributes=True, populate_by_name=True, alias_generator=to_camel
    )
