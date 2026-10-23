"""Internal file/business bindings; callers own locks and transactions."""

from hashlib import sha256

from sqlalchemy import select

from app.core import file_storage
from app.core.exceptions import BusinessRuleException
from app.core.file_storage import is_owned_artifact
from app.modules.system.constants import ExportTaskStatus, ImportBatchStatus
from app.modules.system.models.user_transfer import UserExportTask, UserImportBatch


def artifact_unverified():
    return BusinessRuleException(
        "文件与业务完成事实尚待核对", error_code="AI_ARTIFACT_UNVERIFIED"
    )


def batch_snapshot(batch):
    return {
        "batch_id": batch.batch_id,
        "file_sha256": batch.file_sha256,
        "records_hash": batch.records_hash,
        "operator_id": batch.operator_id,
        "total": batch.total_rows,
        "summary": {
            "new": batch.summary_new,
            "exists": batch.summary_exists,
            "conflict": batch.summary_conflict,
            "outOfScope": batch.summary_out_of_scope,
        },
    }


async def read_batch(db, batch_id, tenant_id, *, lock=False):
    query = (
        select(UserImportBatch)
        .where(
            UserImportBatch.tenant_id == tenant_id,
            UserImportBatch.batch_id == batch_id,
        )
        .execution_options(populate_existing=True)
    )
    return await db.scalar(query.with_for_update() if lock else query)


async def verify_file(key, *, tenant_id, resource_id, role, execution=None):
    storage = file_storage.get_file_storage()
    if not key or not is_owned_artifact(key):
        raise artifact_unverified()
    owner = await storage.artifact_identity(key)
    if (
        owner.tenant_id != tenant_id
        or owner.resource_id != resource_id
        or owner.role != role
        or (execution is not None and owner != execution.identity(role, resource_id))
    ):
        raise artifact_unverified()
    return await storage.read_artifact(key, identity=owner)


async def validate_file_completion(db, *, action, result, execution):
    """A batch replay or an unrelated file is not evidence for this PA."""
    refs = result.projection.subject_refs if result.projection else ()
    if len(refs) != 1:
        raise artifact_unverified()
    ref = refs[0]
    if action.execute_tool_name == "user.export":
        if ref.get("type") != "user_export_task":
            raise artifact_unverified()
        task = await db.scalar(
            select(UserExportTask)
            .where(
                UserExportTask.tenant_id == action.tenant_id,
                UserExportTask.export_id == ref.get("id"),
            )
            .with_for_update()
        )
        if (
            task is None
            or task.operator_id != action.user_id
            or task.status != ExportTaskStatus.SUCCESS
            or result.data.get("exportId") != task.export_id
        ):
            raise artifact_unverified()
        data = await verify_file(
            task.file_storage_key,
            tenant_id=task.tenant_id,
            resource_id=task.export_id,
            role="export",
            execution=execution,
        )
        if task.file_size_bytes != len(data):
            raise artifact_unverified()
        return
    if ref.get("type") != "user_import_batch":
        raise artifact_unverified()
    batch = await read_batch(db, ref.get("id"), action.tenant_id, lock=True)
    if batch is None or batch.operator_id != action.user_id:
        raise artifact_unverified()
    if action.execute_tool_name == "user.import_preview":
        if batch.status != ImportBatchStatus.PREVIEW_DONE:
            raise artifact_unverified()
        data = await verify_file(
            batch.file_storage_key,
            tenant_id=batch.tenant_id,
            resource_id=batch.batch_id,
            role="preview_source",
            execution=execution,
        )
        if sha256(data).hexdigest() != batch.file_sha256:
            raise artifact_unverified()
        return
    if (
        action.subject_ref != ref
        or batch.status
        not in {
            ImportBatchStatus.SUCCESS,
            ImportBatchStatus.PARTIAL_SUCCESS,
            ImportBatchStatus.FAILED,
            ImportBatchStatus.CANCELLED,
        }
        or batch.preview_token != action.frozen_args.get("preview_token")
        or batch.reason != action.frozen_args.get("reason")
        or batch.on_conflict != action.frozen_args.get("on_conflict")
    ):
        raise artifact_unverified()
    # No second execution may turn an old batch replay into a fresh PA success.
    snapshot = dict(action.snapshot)
    snapshot.pop("_authorization", None)
    if snapshot != batch_snapshot(batch):
        raise artifact_unverified()
    if batch.failed_rows_file:
        await verify_file(
            batch.failed_rows_file,
            tenant_id=batch.tenant_id,
            resource_id=batch.batch_id,
            role="import_errors",
            execution=execution,
        )
