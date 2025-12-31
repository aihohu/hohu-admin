from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_serializer
from pydantic.alias_generators import to_camel


class RoleBase(BaseModel):
    role_name: str
    role_code: str
    role_desc: str | None = None
    status: str = "1"  # "1"-启用, "2"-禁用

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class RoleCreate(RoleBase):
    pass


class RoleUpdate(BaseModel):
    role_name: str | None = None
    role_code: str | None = None
    role_desc: str | None = None
    status: str | None = None

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


# class RoleUpdateMenu(BaseModel):


class RoleOut(RoleBase):
    role_id: int
    create_time: datetime

    @field_serializer("role_id")
    def serialize_id(self, role_id: int, _info):
        return str(role_id)

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class RoleQuery(BaseModel):
    current: int = 1
    size: int = 10
    role_name: str | None = None
    role_code: str | None = None
    status: str | None = None

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class RoleSimpleOut(BaseModel):
    role_id: int
    role_name: str
    role_code: str

    @field_serializer("role_id")
    def serialize_id(self, role_id: int, _info):
        return str(role_id)

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
