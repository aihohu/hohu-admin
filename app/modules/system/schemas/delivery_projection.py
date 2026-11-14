"""Typed sparse delivery DTOs never materialize missing fields as null."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, StrictStr, create_model
from pydantic.alias_generators import to_camel

from app.core.base_response import PageResult
from app.modules.system.field_policy.delivery_catalog import delivery_catalog
from app.modules.system.schemas.transfer_projection import SparseTransferOut


class DeliveryFieldPolicy(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )
    read: list[str]
    query: list[str]
    create: list[str] = []
    update: list[str] = []


def _model(name, obj, anchor, **extra):
    types = {
        "opaque_id": StrictStr,
        "id": StrictStr,
        "string": str,
        "integer": int,
        "boolean": bool,
        "datetime": datetime,
        "json": dict,
    }
    fields = {
        f.field_id: (types[f.value_type] | None, None)
        for f in delivery_catalog().objects[obj].fields
    }
    fields[anchor] = (StrictStr, ...)
    fields["field_policy"] = (DeliveryFieldPolicy, ...)
    fields.update(extra)
    return create_model(name, __base__=SparseTransferOut, **fields)


ProjectedOperationLog = _model(
    "ProjectedOperationLog", "system.operation_log", "operation_log_id"
)
ProjectedLoginLog = _model("ProjectedLoginLog", "system.login_log", "login_log_id")
ProjectedJobLog = _model("ProjectedJobLog", "job.log", "job_log_id")
ProjectedAgent = _model("ProjectedAgent", "ai.agent", "agent_id")
ProjectedConversation = _model(
    "ProjectedConversation",
    "ai.conversation",
    "conversation_id",
    model_name=(StrictStr | None, None),
)
ProjectedFeedback = _model(
    "ProjectedFeedback",
    "ai.routing_feedback",
    "feedback_id",
    original_agent_name=(str | None, None),
    corrected_agent_name=(str | None, None),
)


class DeliveryPage[T](PageResult[T]):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )
    field_policy: DeliveryFieldPolicy
