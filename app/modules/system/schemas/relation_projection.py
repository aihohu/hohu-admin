"""Closed sparse relation DTOs; absence survives FastAPI response validation."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictStr, create_model, model_serializer
from pydantic.alias_generators import to_camel

from app.core.config import settings
from app.modules.system.field_policy.ordinary_catalog import relation_catalog
from app.modules.system.schemas.relation_policy import RelationFieldPolicy


class SparseRelationOut(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )

    @model_serializer(mode="wrap")
    def preserve_presence(self, handler, info):
        result = handler(self)
        allowed = {
            type(self).model_fields[n].serialization_alias
            or type(self).model_fields[n].alias
            or n
            if info.by_alias
            else n
            for n in self.model_fields_set
        }
        for name in self.model_fields_set:
            original = getattr(self, name)
            if isinstance(original, datetime):
                key = to_camel(name) if info.by_alias else name
                result[key] = original.strftime(settings.DATETIME_FORMAT)
        return {key: value for key, value in result.items() if key in allowed}


ROLE_SUMMARY_FIELDS = frozenset(
    {"role_id", "role_code", "role_name", "status", "data_scope", "i18n_keys"}
)
ROLE_DETAIL_FIELDS = ROLE_SUMMARY_FIELDS | {"role_desc", "create_time", "dept_ids"}
MENU_DETAIL_FIELDS = frozenset(
    "parent_id menu_name menu_type icon icon_type path component route_name route_path i18n_key order status active_menu fixed_index_in_tab hide_in_menu href keep_alive constant layout multi_tab page path_param menu_id create_time".split()
)
MENU_SIMPLE_FIELDS = frozenset({"menu_id", "menu_name", "create_time"})


def projection(name, obj, anchor, *, only=None, **extras):
    kinds = {
        "id": StrictStr,
        "string": str,
        "integer": int,
        "boolean": bool,
        "datetime": datetime,
        "json": Any,
    }
    fields = {
        f.field_id: (kinds[f.value_type] | None, None)
        for f in relation_catalog().objects[obj].fields
        if only is None or f.field_id in only
    }
    fields[anchor] = (StrictStr, ...)
    fields["field_policy"] = (RelationFieldPolicy | None, None)
    fields.update(extras)
    return create_model(name, __base__=SparseRelationOut, **fields)


ProjectedRoleOut = projection(
    "ProjectedRoleOut",
    "system.role",
    "role_id",
    only=ROLE_DETAIL_FIELDS,
)
ProjectedRoleSummaryOut = projection(
    "ProjectedRoleSummaryOut",
    "system.role",
    "role_id",
    only=ROLE_SUMMARY_FIELDS,
    delegable=(bool, ...),
    blocked_reason_code=(str | None, None),
)
ProjectedAssignableRoleOut = projection(
    "ProjectedAssignableRoleOut",
    "system.role",
    "role_id",
    only={"role_id", "role_code", "role_name", "i18n_keys"},
)
ProjectedDeptOut = projection(
    "ProjectedDeptOut",
    "system.dept",
    "dept_id",
    children=(list["ProjectedDeptOut"] | None, None),
)
ProjectedMenuOut = projection(
    "ProjectedMenuOut",
    "system.menu",
    "menu_id",
    only=MENU_DETAIL_FIELDS,
    children=(list["ProjectedMenuOut"] | None, None),
    buttons=(list[dict[str, str | None]] | None, None),
)
ProjectedMenuSimpleOut = projection(
    "ProjectedMenuSimpleOut", "system.menu", "menu_id", only=MENU_SIMPLE_FIELDS
)
ProjectedDeptOut.model_rebuild()
ProjectedMenuOut.model_rebuild()


class ProjectedTreeOptionOut(SparseRelationOut):
    id: StrictStr
    label: str
    p_id: str
    i18n_key: str | None = None
    children: list["ProjectedTreeOptionOut"]


def serialize_relation(operation, result):
    """Finish closed schema validation and wire serialization before rollback."""
    from pydantic import TypeAdapter  # noqa: PLC0415

    surface, kind = operation.split(".")
    if surface == "runtime" or kind in {"exists", "menus", "agents", "pages"}:
        return result
    model = {
        "role": ProjectedRoleOut,
        "dept": ProjectedDeptOut,
        "menu": ProjectedMenuOut,
    }[surface]
    if surface == "role" and kind in {"list", "all"}:
        model = ProjectedRoleSummaryOut
    if kind == "assignable":
        model = ProjectedAssignableRoleOut
    if surface == "menu" and kind == "all":
        model = ProjectedMenuSimpleOut
    if kind == "options":
        model = ProjectedTreeOptionOut
    if kind in {"list", "tree-list"}:
        return {
            **{key: value for key, value in result.items() if key != "field_policy"},
            "fieldPolicy": RelationFieldPolicy.model_validate(
                result["field_policy"]
            ).model_dump(mode="json", by_alias=True),
            "records": [
                model.model_validate(row).model_dump(mode="json", by_alias=True)
                for row in result["records"]
            ],
        }
    if kind == "detail":
        return model.model_validate(result).model_dump(mode="json", by_alias=True)
    adapter = TypeAdapter(list[model])
    return adapter.dump_python(
        adapter.validate_python(result), mode="json", by_alias=True
    )
