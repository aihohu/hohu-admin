"""Advisory relation capabilities serialized at the protected read boundary."""

from typing import Literal

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.core.base_response import PageResult


class RelationFieldPolicy(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )
    read: list[str]
    read_levels: dict[str, Literal["plain", "masked", "hidden"]]
    query: list[str]
    create: list[str]
    update: list[str]
    special_create: list[str]
    special_update: list[str]
    can_create: bool
    complete: dict[str, bool]


class RelationPage[T](PageResult[T]):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    field_policy: RelationFieldPolicy
