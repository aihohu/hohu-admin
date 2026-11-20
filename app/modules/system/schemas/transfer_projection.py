"""Presence-preserving task metadata, with no free-text or storage backdoors."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, StrictStr, create_model, model_serializer
from pydantic.alias_generators import to_camel

from app.core.base_response import PageResult
from app.modules.system.field_policy.transfer_catalog import transfer_catalog


class TransferFieldPolicy(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )
    read: list[str]
    query: list[str]
    can_download: bool
    can_cancel: bool


class SparseTransferOut(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )

    @model_serializer(mode="wrap")
    def preserve_presence(self, handler, info):
        result = handler(self)
        allowed = {
            to_camel(name) if info.by_alias else name for name in self.model_fields_set
        }
        return {key: value for key, value in result.items() if key in allowed}


def _model(name, obj, anchor, **extra):
    types = {
        "opaque_id": StrictStr,
        "id": StrictStr,
        "string": str,
        "integer": int,
        "datetime": datetime,
        "json": dict,
    }
    fields = {
        field.field_id: (types[field.value_type] | None, None)
        for field in transfer_catalog().objects[obj].fields
    }
    fields[anchor] = (StrictStr, ...)
    fields["field_policy"] = (TransferFieldPolicy, ...)
    return create_model(name, __base__=SparseTransferOut, **fields, **extra)


ProjectedImportBatch = _model(
    "ProjectedImportBatch",
    "transfer.import",
    "batch_id",
    failed_rows_file=(str | None, None),
    expires_at=(datetime | None, None),
)
ProjectedExportTask = _model("ProjectedExportTask", "transfer.export", "export_id")
ProjectedTransferAudit = _model("ProjectedTransferAudit", "transfer.audit", "log_id")


class TransferPage[T](PageResult[T]):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid"
    )
    field_policy: TransferFieldPolicy
