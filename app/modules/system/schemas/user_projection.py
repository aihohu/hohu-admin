"""Projected fields remain absent through FastAPI response validation."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictStr, model_serializer
from pydantic.alias_generators import to_camel

from app.modules.system.schemas.user import UserDeptItem


class FieldAccessOut(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )
    read: Literal["hidden", "masked", "plain"]
    mask_id: Literal["phone/v1", "email/v1"] | None = None
    create: bool | None = None
    update: bool | None = None
    replace: bool | None = None
    query_ops: list[Literal["exact", "contains", "eq", "group", "distinct"]] | None = (
        None
    )

    @model_serializer(mode="wrap")
    def preserve_presence(self, handler):
        allowed = self.model_fields_set | {
            to_camel(name) for name in self.model_fields_set
        }
        return {key: value for key, value in handler(self).items() if key in allowed}


class ProjectedUserOut(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )

    user_id: StrictStr
    user_name: str | None = None
    nickname: str | None = None
    user_email: str | None = None
    user_phone: str | None = None
    user_gender: str | None = None
    user_avatar: str | None = None
    status: str | None = None
    create_time: str | None = None
    roles: list[str] | None = None
    role_names: list[str] | None = None
    role_name_keys: list[str | None] | None = None
    dept_ids: list[str] | None = None
    dept_names: str | None = None
    primary_dept: str | None = None
    user_depts: list[UserDeptItem] | None = None
    field_access: dict[str, FieldAccessOut] = Field(default_factory=dict)
    is_member: bool | None = None
    is_primary: bool | None = None

    @model_serializer(mode="wrap")
    def preserve_presence(self, handler):
        # Excluding None would erase a legitimate clear value. Required response
        # defaults would instead resurrect hidden keys. Retain only supplied keys.
        allowed = self.model_fields_set | {
            to_camel(name) for name in self.model_fields_set
        }
        return {key: value for key, value in handler(self).items() if key in allowed}


class UserMemberQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dept_id: int = Field(gt=0)
    query: str | None = Field(None, max_length=100)
    current: int = Field(1, ge=1)
    size: int = Field(20, ge=1, le=100)
