"""Sparse closed DTOs: omitted fields stay omitted through HTTP serialization."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, field_serializer
from pydantic.alias_generators import to_camel

from app.core.base_response import PageResult


class OrdinaryCapabilities(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )
    read: list[str] = []
    query: list[str] = []
    create: list[str] = []
    update: list[str] = []
    can_create: bool = False


class OrdinaryPage[T](PageResult[T]):
    field_policy: OrdinaryCapabilities
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )


class OrdinaryProjection(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )
    field_policy: OrdinaryCapabilities | None = None

    @field_serializer("*", check_fields=False)
    def serialize_value(self, value, info):
        if (
            info.field_name
            in {"dict_type_id", "dict_code", "demo_id", "dept_id", "create_by"}
            and value is not None
        ):
            return str(value)
        if isinstance(value, datetime):
            # Existing ordinary tables store local-naive timestamps; preserve
            # that legacy representation until their explicit UTC migration.
            return value.isoformat()
        return value


class DictTypeProjection(OrdinaryProjection):
    dict_type_id: str | int
    dict_name: str | None = None
    dict_type: str | None = None
    status: str | None = None
    remark: str | None = None
    create_time: datetime | None = None


class DictDataProjection(OrdinaryProjection):
    dict_code: str | int
    dict_sort: int | None = None
    dict_label: str | None = None
    dict_value: str | None = None
    dict_type: str | None = None
    css_class: str | None = None
    list_class: str | None = None
    is_default: str | None = None
    status: str | None = None
    create_time: datetime | None = None


class DataScopeDemoProjection(OrdinaryProjection):
    demo_id: str | int
    title: str | None = None
    content: str | None = None
    dept_id: str | int | None = None
    create_by: str | int | None = None
    status: str | None = None
    create_time: datetime | None = None
    update_time: datetime | None = None
