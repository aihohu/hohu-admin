from datetime import datetime
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator
from pydantic.alias_generators import to_camel

from app.constants import STATUS_DISABLED, STATUS_ENABLED
from app.core.config import settings


class ConfigBase(BaseModel):
    """系统配置基础字段"""

    config_name: str = Field(..., min_length=1, max_length=100, description="配置名称")
    config_key: str = Field(..., min_length=1, max_length=100, description="配置键")
    config_value: str = Field(..., description="配置值")
    config_type: str = Field(
        "text", max_length=20, description="配置类型：text/richtext/file"
    )

    VALID_CONFIG_TYPES: ClassVar[set[str]] = {"text", "richtext", "file"}
    config_group: str = Field(..., min_length=1, max_length=50, description="配置分组")
    status: str = Field(..., description="状态：1-启用，2-禁用")
    is_public: bool = Field(False, description="是否公开访问")
    remark: str | None = Field(None, max_length=500, description="备注")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    @field_validator("config_name")
    @classmethod
    def validate_config_name(cls, v: str) -> str:
        """验证配置名称"""
        if not v or v.strip() == "":
            raise ValueError("配置名称不能为空")
        return v.strip()

    @field_validator("config_key")
    @classmethod
    def validate_config_key(cls, v: str) -> str:
        """验证配置键"""
        if not v or v.strip() == "":
            raise ValueError("配置键不能为空")
        return v.strip()

    @field_validator("config_type")
    @classmethod
    def validate_config_type(cls, v: str) -> str:
        """验证配置类型"""
        if v not in cls.VALID_CONFIG_TYPES:
            raise ValueError(f"配置类型必须是 {', '.join(cls.VALID_CONFIG_TYPES)} 之一")
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str) -> str:
        """验证状态值"""
        if v not in [STATUS_ENABLED, STATUS_DISABLED]:
            raise ValueError("状态必须是 1(启用) 或 2(禁用)")
        return v


class ConfigCreate(ConfigBase):
    """系统配置创建请求"""

    pass


class ConfigUpdate(BaseModel):
    """系统配置更新请求"""

    config_name: str | None = Field(
        None, min_length=1, max_length=100, description="配置名称"
    )
    config_key: str | None = Field(
        None, min_length=1, max_length=100, description="配置键"
    )
    config_value: str | None = Field(None, description="配置值")
    config_type: str | None = Field(None, max_length=20, description="配置类型")
    config_group: str | None = Field(
        None, min_length=1, max_length=50, description="配置分组"
    )
    status: str | None = Field(None, description="状态：1-启用，2-禁用")
    is_public: bool | None = Field(None, description="是否公开访问")
    remark: str | None = Field(None, max_length=500, description="备注")

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)

    @field_validator("config_name")
    @classmethod
    def validate_config_name(cls, v: str | None) -> str | None:
        """验证配置名称（可选字段）"""
        if v is not None and (not v or v.strip() == ""):
            raise ValueError("配置名称不能为空")
        return v.strip() if v is not None else None

    @field_validator("config_key")
    @classmethod
    def validate_config_key(cls, v: str | None) -> str | None:
        """验证配置键（可选字段）"""
        if v is not None and (not v or v.strip() == ""):
            raise ValueError("配置键不能为空")
        return v.strip() if v is not None else None

    @field_validator("config_type")
    @classmethod
    def validate_config_type(cls, v: str | None) -> str | None:
        """验证配置类型（可选字段）"""
        if v is not None and v not in ConfigBase.VALID_CONFIG_TYPES:
            raise ValueError(
                f"配置类型必须是 {', '.join(ConfigBase.VALID_CONFIG_TYPES)} 之一"
            )
        return v

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        """验证状态值（可选字段）"""
        if v is not None and v not in [STATUS_ENABLED, STATUS_DISABLED]:
            raise ValueError("状态必须是 1(启用) 或 2(禁用)")
        return v


class ConfigOut(ConfigBase):
    """系统配置输出"""

    config_id: int
    create_time: datetime
    update_time: datetime

    @field_serializer("config_id")
    def serialize_id(self, config_id: int, _info):
        return str(config_id)

    @field_serializer("create_time")
    def serialize_create_time(self, dt: datetime) -> str:
        """格式化创建时间"""
        return dt.strftime(settings.DATETIME_FORMAT)

    @field_serializer("update_time")
    def serialize_update_time(self, dt: datetime) -> str:
        """格式化更新时间"""
        return dt.strftime(settings.DATETIME_FORMAT)

    model_config = ConfigDict(
        from_attributes=True, populate_by_name=True, alias_generator=to_camel
    )


class ConfigQuery(BaseModel):
    """系统配置查询参数"""

    current: int = Field(1, ge=1, description="当前页码")
    size: int = Field(10, ge=1, le=100, description="每页数量")
    config_name: str | None = Field(None, description="配置名称（支持模糊查询）")
    config_key: str | None = Field(None, description="配置键（支持模糊查询）")
    config_group: str | None = Field(None, description="配置分组（支持模糊查询）")
    status: str | None = Field(None, description="状态")

    @field_validator("status")
    @classmethod
    def validate_status(cls, v: str | None) -> str | None:
        """验证状态值（可选字段）"""
        if v is not None and v not in [STATUS_ENABLED, STATUS_DISABLED]:
            raise ValueError("状态必须是 1(启用) 或 2(禁用)")
        return v

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
