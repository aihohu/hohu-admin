"""Closed control forms and explicit publication confirmation commands."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from app.modules.system.schemas.control_config import (
    ControlConfigCreate,
    ControlConfigUpdate,
)


class ParameterFormQuery(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )
    mode: Literal["create", "update"]
    record_id: str | None = Field(None, pattern=r"^[1-9][0-9]{0,18}$")

    @model_validator(mode="after")
    def target(self):
        if (
            (self.mode == "update") != (self.record_id is not None)
            or self.record_id is not None
            and int(self.record_id) >= 2**63
        ):
            raise ValueError("Invalid form target")
        return self


class ParameterPublicationWrite(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", strict=True
    )
    confirmed: bool
    expected_revision: str = Field(min_length=1, max_length=2048)

    @model_validator(mode="after")
    def confirmation(self):
        if self.confirmed is not True:
            raise ValueError("Explicit confirmation required")
        return self


class ParameterRevocationWrite(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", strict=True
    )
    expected_revision: str = Field(min_length=1, max_length=2048)


class ParameterConfigCreate(ControlConfigCreate):
    form_receipt: str | None = Field(None, max_length=2048, repr=False)


class ParameterConfigUpdate(ControlConfigUpdate):
    form_receipt: str | None = Field(None, max_length=2048, repr=False)
