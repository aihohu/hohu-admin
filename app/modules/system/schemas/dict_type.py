from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator
from pydantic.alias_generators import to_camel

from app.constants import STATUS_DISABLED, STATUS_ENABLED
from app.core.config import settings


class DictTypeBase(BaseModel):
    """字典类型基础字段"""

    dict_name: str = Field(..., min_length=2, max_length=100, description="字典名称")
    dict_type: str = Field(..., min_length=2, max_length=100, description="字典类型")
    status: str = Field(
        ...,
        description="状态：1-启用，2-禁用",
    )
    remark: str | None = Field(None, max_length=500, description="备注")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    @field_validator("dict_name")
    @classmethod
    def validate_dict_name(cls, v: str) -> str:
        """验证字典名称"""
        if not v or v.strip() == "":
            raise ValueError("字典名称不能为空")
        return v.strip()

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        """验证状态值"""
        if v not in [STATUS_ENABLED, STATUS_DISABLED]:
            raise ValueError("状态必须是 1(启用) 或 2(禁用)")
        return v


class DictTypeCreate(DictTypeBase):
    """字典类型创建请求"""

    pass


class DictTypeUpdate(BaseModel):
    """字典类型更新请求"""

    dict_name: str | None = Field(
        None, min_length=2, max_length=100, description="字典名称"
    )
    dict_type: str | None = Field(
        None, min_length=2, max_length=100, description="字典类型"
    )
    status: str | None = Field(None, description="状态：1-启用，2-禁用")
    remark: str | None = Field(None, max_length=500, description="备注")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    @field_validator("dict_name")
    @classmethod
    def validate_dict_name(cls, v: str | None) -> str | None:
        """验证字典名称（可选字段）"""
        if v is not None and (not v or v.strip() == ""):
            raise ValueError("字典名称不能为空")
        return v.strip() if v is not None else None

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        """验证状态值（可选字段）"""
        if v is not None and v not in [STATUS_ENABLED, STATUS_DISABLED]:
            raise ValueError("状态必须是 1(启用) 或 2(禁用)")
        return v


class DictTypeOut(DictTypeBase):
    """字典类型输出"""

    dict_type_id: int
    create_time: datetime

    @field_serializer("dict_type_id")
    def serialize_id(self, dict_type_id: int, _info):
        return str(dict_type_id)

    @field_serializer("create_time")
    def serialize_create_time(self, dt: datetime) -> str:
        """格式化创建时间"""
        return dt.strftime(settings.DATETIME_FORMAT)

    model_config = ConfigDict(
        from_attributes=True, populate_by_name=True, alias_generator=to_camel
    )


class DictTypeSimpleOut(BaseModel):
    """字典类型简单输出（用于下拉选择）"""

    dict_type_id: int
    dict_name: str
    dict_type: str
    status: str

    @field_serializer("dict_type_id")
    def serialize_id(self, dict_type_id: int, _info):
        return str(dict_type_id)

    model_config = ConfigDict(
        from_attributes=True, populate_by_name=True, alias_generator=to_camel
    )


class DictTypeQuery(BaseModel):
    """字典类型查询参数"""

    current: int = Field(1, ge=1, description="当前页码")
    size: int = Field(10, ge=1, le=100, description="每页数量")
    dict_name: str | None = Field(None, description="字典名称（支持模糊查询）")
    dict_type: str | None = Field(None, description="字典类型（支持模糊查询）")
    status: str | None = Field(None, description="状态")

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        """验证状态值（可选字段）"""
        if v is not None and v not in [STATUS_ENABLED, STATUS_DISABLED]:
            raise ValueError("状态必须是 1(启用) 或 2(禁用)")
        return v

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
