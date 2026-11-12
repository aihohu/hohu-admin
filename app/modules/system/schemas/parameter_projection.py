"""Closed sparse DTOs for control records; internal facts have no output field."""

from datetime import datetime

from pydantic import StrictStr, create_model

from app.modules.system.field_policy.control_catalog import control_catalog
from app.modules.system.schemas.relation_policy import RelationFieldPolicy
from app.modules.system.schemas.relation_projection import SparseRelationOut


def _projection(name, obj, anchor):
    kinds = {
        "id": StrictStr,
        "string": str,
        "integer": int,
        "boolean": bool,
        "datetime": datetime,
    }
    fields = {
        f.field_id: (kinds[f.value_type] | None, None)
        for f in control_catalog().objects[obj].fields
    }
    fields[anchor] = (StrictStr, ...)
    fields["field_policy"] = (RelationFieldPolicy | None, None)
    return create_model(name, __base__=SparseRelationOut, **fields)


ProjectedConfigOut = _projection("ProjectedConfigOut", "system.config", "config_id")
ProjectedJobOut = _projection("ProjectedJobOut", "job.definition", "job_id")


def serialize_parameter(surface, values, policy):
    model = ProjectedConfigOut if surface == "config" else ProjectedJobOut
    anchor = surface + "_id"
    return model.model_validate(
        {**values, anchor: str(values[anchor]), "field_policy": policy}
    ).model_dump(by_alias=True, exclude_unset=True)
