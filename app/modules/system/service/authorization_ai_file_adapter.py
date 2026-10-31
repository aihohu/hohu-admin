"""Fixed FILE bindings; preparation never grants execution authority."""

import asyncio
from dataclasses import dataclass, replace
from hashlib import sha256

from sqlalchemy import select

from app.core import file_storage
from app.core.authorization_file import FileBudget
from app.core.authorization_plan import ResourceKey, ResourceKind
from app.core.file_storage import ArtifactExecution
from app.db.authorization_owner import rollback_authorization_session
from app.db.authorization_plan_session import _order_error
from app.modules.ai.models.prepared_action import AiPreparedAction
from app.modules.system.ai_tools.user_transfer import (
    _planned_export_result,
    _planned_import_result,
    _user_import_mime_for_filename,
)
from app.modules.system.constants import EmployeeNoSyncMode, ImportBatchStatus
from app.modules.system.models.user_transfer import UserImportBatch
from app.modules.system.schemas.user_transfer import UserExportFilter
from app.modules.system.service.authorization_ai_user_input import context
from app.modules.system.service.authorization_import_candidate import (
    import_authorization_candidate,
)
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transfer_candidate import (
    transfer_candidate,
)
from app.modules.system.service.user_import_parser import (
    import_file_has_column,
    parse_import_excel,
)
from app.modules.system.service.user_transfer_artifact_service import batch_snapshot

FILE_EXECUTIONS = frozenset({"user.export", "user.import_execute"})


@dataclass(frozen=True, slots=True, repr=False)
class FileInput:
    values: tuple
    execution: ArtifactExecution
    command: object
    storage: object
    snapshot: object


class AIFileAdapter:
    async def prepare_input(
        self, db, *, tool, args, snapshot, tenant, actor_user_version, execution
    ):
        if (
            type(execution) is not ArtifactExecution
            or execution.tenant_id != tenant.tenant_id
        ):
            raise _order_error()
        storage = file_storage.get_file_storage()
        if tool == "user.export":
            command = transfer_candidate.prepare_export(
                tenant=tenant,
                actor_user_version=actor_user_version,
                args=args,
                execution=execution,
            )
        elif tool == "user.import_execute":
            if (
                set(args) - {"preview_token", "reason", "on_conflict", "sync_mode"}
                or not {"preview_token", "reason"} <= args.keys()
            ):
                raise _order_error()
            try:
                with (
                    bounded_authorization_reads(db),
                    FileBudget(
                        max_reads=2, max_total_bytes=20 * 1024 * 1024
                    ).activate(),
                ):
                    async with asyncio.timeout(5):
                        batch = await db.scalar(
                            select(UserImportBatch).where(
                                UserImportBatch.tenant_id == tenant.tenant_id,
                                UserImportBatch.preview_token == args["preview_token"],
                            )
                        )
                        if (
                            batch is None
                            or batch.operator_id != tenant.actor_user_id
                            or batch.status != ImportBatchStatus.PREVIEW_DONE
                            or batch_snapshot(batch) != snapshot
                        ):
                            raise _order_error()
                        key, filename, expected_hash = (
                            batch.file_storage_key,
                            batch.filename,
                            batch.file_sha256,
                        )
                        source_identity = await storage.artifact_identity(key)
                        if (
                            source_identity.tenant_id != tenant.tenant_id
                            or source_identity.resource_id != batch.batch_id
                            or source_identity.role != "preview_source"
                        ):
                            raise _order_error()
                        data = await storage.read_artifact(
                            key, identity=source_identity
                        )
                        if sha256(data).hexdigest() != expected_hash:
                            raise _order_error()
            finally:
                await rollback_authorization_session(db)
            mime = _user_import_mime_for_filename(filename)
            command = await import_authorization_candidate.prepare_input(
                db,
                tenant=tenant,
                actor_user_version=actor_user_version,
                records=parse_import_excel(data, mime),
                file_bytes=data,
                preview_token=args["preview_token"],
                filename=filename,
                reason=args["reason"],
                on_conflict=args.get("on_conflict", "skip"),
                sync_mode=EmployeeNoSyncMode(args.get("sync_mode", "CREATE_ONLY")),
                has_role_column=import_file_has_column(data, mime, "role_input"),
                file_storage=storage,
                artifact_execution=execution,
            )
            command = replace(
                command, source_proof=(key, source_identity, expected_hash)
            )
        else:
            raise _order_error()
        return FileInput(
            (tenant, actor_user_version, tool), execution, command, storage, snapshot
        )

    async def plan(self, db, tx, command):
        if type(command) is not FileInput:
            raise _order_error()
        if command.values[2] == "user.export":
            return await transfer_candidate.plan(db, tx, command.command)
        batch = await db.scalar(
            select(UserImportBatch)
            .where(
                UserImportBatch.tenant_id == command.values[0].tenant_id,
                UserImportBatch.preview_token == command.command.values[3],
            )
            .execution_options(populate_existing=True)
        )
        if (
            batch is None
            or batch.status != ImportBatchStatus.PREVIEW_DONE
            or batch_snapshot(batch) != command.snapshot
        ):
            raise _order_error()
        return await import_authorization_candidate.plan(db, tx, command.command)

    async def apply(self, db, tx, command, work):
        if type(command) is not FileInput or work.command != command.command:
            raise _order_error()
        if command.values[2] == "user.export":
            result = await transfer_candidate.apply(
                db, tx, work, storage=command.storage
            )
            ctx = await context(db, command.values[0], "user.export")
            from app.modules.ai.service.result_projection_service import (  # noqa: PLC0415
                result_projection_service,
            )

            ctx.data_scope_hash = (
                await result_projection_service.compute_data_scope_hash(db, ctx.user)
            )
            action_id = int(command.execution.operation_id)
            tx.assert_held(
                ResourceKey(
                    ResourceKind.PREPARED_ACTION, command.values[0].tenant_id, action_id
                )
            )
            action = await db.get(AiPreparedAction, action_id, populate_existing=True)
            if (
                action is None
                or action.execution_owner != command.execution.execution_owner
                or action.status != "running"
            ):
                raise _order_error()
            dependencies = result_projection_service.projection_dependency_message_ids(
                action
            )
            if dependencies is None:
                raise _order_error()
            ctx.projection_dependency_message_ids = list(dependencies)
            return await _planned_export_result(
                ctx,
                UserExportFilter.model_validate_json(command.command.values[0]),
                result.count,
                result.resource_id,
            )
        result = await import_authorization_candidate.apply(db, tx, work)
        if result.idempotent_replay:
            raise _order_error()
        return _planned_import_result(result)


ai_file_adapter = AIFileAdapter()
