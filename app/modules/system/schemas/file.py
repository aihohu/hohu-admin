from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer
from pydantic.alias_generators import to_camel

from app.core.config import settings


class FileOut(BaseModel):
    """文件信息输出"""

    file_id: int
    original_name: str
    file_name: str
    file_path: str
    file_url: str
    file_size: int
    file_ext: str
    mime_type: str | None = None
    business_type: str | None = None
    business_id: int | None = None
    create_by: str | None = None
    create_time: datetime

    @field_serializer("file_id", "business_id")
    def serialize_id(self, v: int | None, _info):
        return str(v) if v is not None else None

    @field_serializer("create_time")
    def serialize_create_time(self, dt: datetime) -> str:
        return dt.strftime(settings.DATETIME_FORMAT)

    model_config = ConfigDict(
        from_attributes=True, populate_by_name=True, alias_generator=to_camel
    )


class FileQuery(BaseModel):
    """文件查询参数"""

    current: int = Field(1, ge=1, description="当前页码")
    size: int = Field(10, ge=1, le=100, description="每页数量")
    original_name: str | None = Field(None, description="原始文件名(支持模糊查询)")
    business_type: str | None = Field(None, description="业务类型")
    business_id: int | None = Field(None, description="业务记录ID")
    file_ext: str | None = Field(None, description="文件扩展名")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
