"""Protected task metadata; prove the unfiltered owner domain before counts."""

from datetime import timedelta
from enum import Enum

from sqlalchemy import and_, case, func, literal, select, true

from app.core.exceptions import (
    AuthorizationException,
    BusinessRuleException,
    NotFoundException,
)
from app.core.field_policy import project
from app.core.field_policy.contracts import require
from app.core.rbac import is_super_admin
from app.modules.system.constants import ExportTaskStatus, ImportBatchStatus
from app.modules.system.field_policy.transfer_catalog import EQ, RANGE, READ
from app.modules.system.models.user import User
from app.modules.system.models.user_transfer import (
    UserExportTask,
    UserImportBatch,
    UserImportBatchLog,
)
from app.modules.system.schemas.transfer_projection import (
    ProjectedExportTask,
    ProjectedImportBatch,
    ProjectedTransferAudit,
)
from app.modules.system.schemas.user_transfer import (
    UserExportTaskQuery,
    UserImportBatchQuery,
)
from app.modules.system.service.relation_read_context import (
    RelationReadContext,
    detached,
    load_relation_read_context,
)


class TransferMetaContext(RelationReadContext):
    batch_id = None

    def scope_sql(self, obj, table):
        if obj.startswith("transfer."):
            return dict.fromkeys(
                self.scopes,
                true()
                if obj == "transfer.audit" or is_super_admin(self.actor)
                else table.c.operator_id == self.actor.user_id,
            )
        return super().scope_sql(obj, table)

    def base(self, obj, table):
        base = super().base(obj, table)
        return (
            and_(base, table.c.batch_id == self.batch_id)
            if obj == "transfer.audit"
            else base
        )

    def policy(self, record, *, present=None, download=False, cancel=False):
        decisions = self.decisions(record)
        return {
            "read": sorted(
                name
                for name, d in decisions.items()
                if d.allowed and (present is None or name in present)
            ),
            "query": sorted(
                {
                    use.ref.field_id
                    for use in self.catalog.actions[self.action].uses
                    if use.ref.object_id == record.object_id
                    and use.access.purpose == "management.query"
                    and self.decisions(record, use.access)[use.ref.field_id].allowed
                }
            ),
            "can_download": download,
            "can_cancel": cancel,
        }


async def transfer_metadata(db, tx, *, tenant, version, operation, query, identifier):
    model, obj, anchor, action, schema = (
        (
            UserExportTask,
            "transfer.export",
            "export_id",
            "transfer.export-list",
            ProjectedExportTask,
        )
        if operation in {"exports", "export"}
        else (
            UserImportBatchLog,
            "transfer.audit",
            "log_id",
            "transfer.audit-list",
            ProjectedTransferAudit,
        )
        if operation == "logs"
        else (
            UserImportBatch,
            "transfer.import",
            "batch_id",
            "transfer.import-list",
            ProjectedImportBatch,
        )
    )
    original = await load_relation_read_context(
        db,
        tx,
        tenant=tenant,
        actor_user_version=version,
        action=action,
        objects={obj, "system.user"},
    )
    context = TransferMetaContext(
        original.tenant,
        original.actor,
        original.rules,
        original.scopes,
        original.permissions,
        original.action,
    )
    table = model.__table__
    predicates = []
    dependencies = []
    if operation == "logs":
        require(
            type(query) is dict
            and set(query) == {"event", "current", "size"}
            and type(query["current"]) is int
            and query["current"] >= 1
            and type(query["size"]) is int
            and 1 <= query["size"] <= 100
        )
        parent = await db.scalar(
            select(UserImportBatch.batch_id).where(
                context.base("transfer.import", UserImportBatch.__table__),
                UserImportBatch.batch_id == identifier,
            )
        )
        if parent is None:
            raise NotFoundException(
                "用户导入批次", error_code="AI_IMPORT_BATCH_NOT_FOUND"
            )
        context.batch_id = identifier
        current, size = query["current"], query["size"]
        if query["event"]:
            dependencies.append(("event", EQ))
            predicates.append(table.c.event == query["event"])
    elif operation in {"exports", "batches"}:
        expected = (
            UserExportTaskQuery if operation == "exports" else UserImportBatchQuery
        )
        require(type(query) is expected)
        query = expected.model_validate(query.model_dump())
        current, size = query.current, query.size
        if query.operator_id is not None:
            if (
                not is_super_admin(context.actor)
                and query.operator_id != tenant.actor_user_id
            ):
                raise AuthorizationException(
                    error_code="AI_RESULT_PROJECTION_FORBIDDEN"
                )
            predicates.append(table.c.operator_id == query.operator_id)
        if query.status:
            try:
                status = (
                    ExportTaskStatus if operation == "exports" else ImportBatchStatus
                )(query.status)
            except ValueError:
                raise BusinessRuleException(
                    "状态筛选无效",
                    error_code="AI_EXPORT_INVALID_STATUS"
                    if operation == "exports"
                    else "AI_IMPORT_INVALID_STATUS",
                ) from None
            predicates.append(table.c.status == status)
            dependencies.append(("status", EQ))
        if operation == "batches":
            for name, lower in (("start_time", True), ("end_time", False)):
                value = getattr(query, name)
                if value is not None:
                    if ("created_at", RANGE) not in dependencies:
                        dependencies.append(("created_at", RANGE))
                    predicates.append(
                        table.c.created_at >= value
                        if lower
                        else table.c.created_at <= value
                    )
    else:
        current, size = 1, 1
        predicates.append(table.c[anchor] == identifier)
    await context.prove(db, obj, table, tuple(dependencies))
    many = operation in {"exports", "batches", "logs"}
    total = (
        await db.scalar(
            select(func.pg_catalog.count(1))
            .select_from(table)
            .where(context.base(obj, table), *predicates)
        )
        if many
        else None
    )
    fields = tuple(
        field
        for field in context.catalog.objects[obj].fields
        if field.field_id in table.c or field.field_id in {"filename", "counts"}
    )
    columns = []
    for field in fields:
        name = field.field_id
        value = (
            literal("users.xlsx")
            if name == "filename"
            else table.c.detail
            if name == "counts"
            else table.c[name]
        )
        columns.append(
            case((context.coverage(obj, table, name), value), else_=None).label(name)
        )
    columns.append(table.c.operator_id.label("_owner"))
    if obj == "transfer.import":
        columns.append(table.c.failed_rows_file.is_not(None).label("_has_errors"))
    rows = (
        (
            await db.execute(
                select(*columns)
                .where(context.base(obj, table), *predicates)
                .order_by(table.c[anchor].asc())
                .offset((current - 1) * size)
                .limit(size)
            )
        )
        .mappings()
        .all()
    )
    if not many and not rows:
        raise NotFoundException(
            "传输任务",
            error_code="AI_EXPORT_TASK_NOT_FOUND"
            if operation == "export"
            else "AI_IMPORT_BATCH_NOT_FOUND",
        )
    ids = {row["_owner"] for row in rows if row["_owner"] is not None}
    owners = (
        await context.fetch(
            db,
            User,
            "user_id",
            predicates=(User.user_id.in_(ids),),
            maximum=101,
            names=("user_name",),
        )
        if ids
        else ()
    )
    names = {
        int(record.record_key): values["user_name"]
        for record, values, _ in owners
        if "user_name" in values
    }
    for record, values, _ in owners:
        if "user_name" in values:
            context.source(record, ("user_name",))
    records = []
    for row in rows:
        record = context.record(obj, str(row[anchor]), context.scopes)
        values = {
            field.field_id: (
                row[field.field_id].value
                if isinstance(row[field.field_id], Enum)
                else row[field.field_id]
            )
            for field in fields
        }
        if "counts" in values:
            detail = values["counts"] or {}
            values["counts"] = {
                key: value
                for key, value in detail.items()
                if key
                in {
                    "chunk_index",
                    "total_rows",
                    "chunk_size",
                    "failed_in_chunk",
                    "total_chunks",
                    "success_count",
                    "overwritten_count",
                    "skipped_count",
                    "failed_count",
                }
                and type(value) is int
                and value >= 0
            }
        if row["_owner"] in names:
            values["operator_name"] = names[row["_owner"]]
        values = detached(
            project(context.catalog, context.rules, record, action, READ, values)
        )
        own = row["_owner"] == tenant.actor_user_id
        export = obj == "transfer.export"
        downloadable = (
            (own or is_super_admin(context.actor))
            and ("system:user:export" if export else "system:user:import")
            in context.permissions
            and "status" in values
            and (
                values["status"] == "SUCCESS"
                if export
                else "failed_count" in values and row.get("_has_errors", False)
            )
        )
        cancel = (
            own
            and "system:user:import" in context.permissions
            and values.get("status") in {"CREATED", "PREVIEW_DONE", "RUNNING"}
        )
        if obj == "transfer.import":
            if downloadable:
                values["failed_rows_file"] = (
                    f"/system/file/import-error/{row[anchor]}.xlsx"
                )
            if {"status", "created_at", "finished_at"} <= values.keys():
                values["expires_at"] = (
                    values["created_at"] + timedelta(minutes=10)
                    if values["status"] in {"CREATED", "PREVIEW_DONE", "RUNNING"}
                    else (values["finished_at"] or values["created_at"])
                    + timedelta(hours=24)
                )
        values["field_policy"] = context.policy(
            record, present=values, download=bool(downloadable), cancel=bool(cancel)
        )
        records.append(
            schema.model_validate(values).model_dump(
                mode="json", by_alias=True, exclude_unset=True
            )
        )
    if many:
        return {
            "records": records,
            "total": total,
            "current": current,
            "size": size,
            "fieldPolicy": context.policy(context.record(obj, "query", context.scopes)),
        }
    return records[0]
