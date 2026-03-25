from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator
from pydantic.alias_generators import to_camel

from app.constants import STATUS_DISABLED, STATUS_ENABLED
from app.core.config import settings


class DictDataBase(BaseModel):
    """字典数据基础字段"""

    dict_sort: int = Field(..., ge=0, description="字典排序")
    dict_label: str = Field(..., min_length=1, max_length=100, description="字典标签")
    dict_value: str = Field(..., min_length=1, max_length=100, description="字典键值")
    dict_type: str = Field(..., min_length=1, max_length=100, description="字典类型")
    css_class: str | None = Field(None, max_length=100, description="样式属性")
    list_class: str | None = Field(None, max_length=100, description="表格回显样式")
    is_default: str = Field(..., description="是否默认：Y-是，N-否")
    status: str = Field(
        ...,
        description="状态：1-启用，2-禁用",
    )

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    @field_validator("dict_label")
    @classmethod
    def validate_dict_label(cls, v: str) -> str:
        """验证字典标签"""
        if not v or v.strip() == "":
            raise ValueError("字典标签不能为空")
        return v.strip()

    @field_validator("is_default")
    @classmethod
    def validate_is_default(cls, v: str) -> str:
        """验证是否默认值"""
        if v not in ["Y", "N"]:
            raise ValueError("是否默认值必须是 Y(是) 或 N(否)")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        """验证状态值"""
        if v not in [STATUS_ENABLED, STATUS_DISABLED]:
            raise ValueError("状态必须是 1(启用) 或 2(禁用)")
        return v


class DictDataCreate(DictDataBase):
    """字典数据创建请求"""

    pass


class DictDataUpdate(BaseModel):
    """字典数据更新请求"""

    dict_sort: int | None = Field(None, ge=0, description="字典排序")
    dict_label: str | None = Field(
        None, min_length=1, max_length=100, description="字典标签"
    )
    dict_value: str | None = Field(
        None, min_length=1, max_length=100, description="字典键值"
    )
    dict_type: str | None = Field(
        None, min_length=1, max_length=100, description="字典类型"
    )
    css_class: str | None = Field(None, max_length=100, description="样式属性")
    list_class: str | None = Field(None, max_length=100, description="表格回显样式")
    is_default: str | None = Field(None, description="是否默认：Y-是，N-否")
    status: str | None = Field(None, description="状态：1-启用，2-禁用")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    @field_validator("dict_label")
    @classmethod
    def validate_dict_label(cls, v: str | None) -> str | None:
        """验证字典标签（可选字段）"""
        if v is not None and (not v or v.strip() == ""):
            raise ValueError("字典标签不能为空")
        return v.strip() if v is not None else None

    @field_validator("is_default")
    @classmethod
    def validate_is_default(cls, v: str | None) -> str | None:
        """验证是否默认值（可选字段）"""
        if v is not None and v not in ["Y", "N"]:
            raise ValueError("是否默认值必须是 Y(是) 或 N(否)")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        """验证状态值（可选字段）"""
        if v is not None and v not in [STATUS_ENABLED, STATUS_DISABLED]:
            raise ValueError("状态必须是 1(启用) 或 2(禁用)")
        return v


class DictDataOut(DictDataBase):
    """字典数据输出"""

    dict_code: int
    create_time: datetime

    @field_serializer("dict_code")
    def serialize_id(self, dict_code: int, _info):
        return str(dict_code)

    @field_serializer("create_time")
    def serialize_create_time(self, dt: datetime) -> str:
        """格式化创建时间"""
        return dt.strftime(settings.DATETIME_FORMAT)

    model_config = ConfigDict(
        from_attributes=True, populate_by_name=True, alias_generator=to_camel
    )


class DictDataQuery(BaseModel):
    """字典数据查询参数"""

    current: int = Field(1, ge=1, description="当前页码")
    size: int = Field(10, ge=1, le=100, description="每页数量")
    dict_label: str | None = Field(None, description="字典标签（支持模糊查询）")
    dict_value: str | None = Field(None, description="字典键值（支持模糊查询）")
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
