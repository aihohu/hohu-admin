"""Current form snapshot and advisory capabilities; never a write credential."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from app.modules.system.schemas.relation_policy import RelationFieldPolicy
from app.modules.system.schemas.relation_projection import (
    MENU_DETAIL_FIELDS,
    ProjectedDeptOut,
    ProjectedRoleOut,
    projection,
)


class RelationFormQuery(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )
    mode: Literal["create", "update"]
    record_id: str | None = Field(None, pattern=r"^[1-9][0-9]*$", max_length=19)
    parent_id: str | None = Field(None, pattern=r"^(0|[1-9][0-9]*)$", max_length=19)

    @model_validator(mode="after")
    def validate_target(self):
        if any(
            value is not None and int(value) >= 2**63
            for value in (self.record_id, self.parent_id)
        ):
            raise ValueError("ID outside the Snowflake range")
        if self.mode == "update":
            if self.record_id is None or self.parent_id is not None:
                raise ValueError("update requires recordId and forbids parentId")
        elif self.record_id is not None:
            raise ValueError("create forbids recordId")
        return self


ProjectedMenuFormOut = projection(
    "ProjectedMenuFormOut",
    "system.menu",
    "menu_id",
    only=MENU_DETAIL_FIELDS | {"query", "permission"},
    buttons=(list[dict[str, str | None]], None),
)


class RelationFormOut[T](BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)
    record: T | None
    field_policy: RelationFieldPolicy


def serialize_form(surface, result):
    model = {
        "role": ProjectedRoleOut,
        "dept": ProjectedDeptOut,
        "menu": ProjectedMenuFormOut,
    }[surface]
    return {
        "record": model.model_validate(result["record"]).model_dump(
            mode="json", by_alias=True
        )
        if result["record"] is not None
        else None,
        "fieldPolicy": RelationFieldPolicy.model_validate(
            result["field_policy"]
        ).model_dump(mode="json", by_alias=True),
    }
