from typing import Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator
from pydantic.alias_generators import to_camel

from app.utils.validators import validate_password, validate_user_name


class EmailIdentity(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )
    tenant_code: str | None = Field(None, max_length=100)
    user_name: str = Field(min_length=2, max_length=16)
    email: EmailStr = Field(max_length=100)

    @field_validator("user_name")
    @classmethod
    def username_valid(cls, value):
        return validate_user_name(value)


class EmailCodeRequest(EmailIdentity):
    purpose: Literal["register", "reset"]


class Registration(EmailIdentity):
    code: str = Field(pattern=r"^[0-9]{6}$")
    password: str = Field(max_length=72)

    @field_validator("password")
    @classmethod
    def password_valid(cls, value):
        return validate_password(value)


class EmailBinding(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )
    email: EmailStr = Field(max_length=100)
    password: str = Field(min_length=1, max_length=72)
    code: str | None = Field(None, pattern=r"^[0-9]{6}$")
