"""Stage deletion intentions only. API/task owners commit before draining them."""

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import select

from app.core import file_storage
from app.core import redis as redis_module
from app.core.exceptions import BusinessRuleException
from app.modules.system.constants import ImportBatchStatus
from app.modules.system.models.user_transfer import UserExportTask, UserImportBatch

logger = logging.getLogger(__name__)
_QUEUE = "transfer_cleanup_intentions"

if TYPE_CHECKING:
    from app.modules.ai.service.file_cleanup_dependency_service import TransferResource


@dataclass(frozen=True)
class CleanupIntent:
    resource: "TransferResource"
    storage: object
    files: tuple
    preview_token: str | None


async def cleanup_resource(row, *, storage=None):
    from app.modules.ai.service.file_cleanup_dependency_service import (  # noqa: PLC0415
        TransferResource,
    )

    storage = storage or file_storage.get_file_storage()
    batch = isinstance(row, UserImportBatch)
    resource_id = row.batch_id if batch else row.export_id
    kind = "user_import_batch" if batch else "user_export_task"
    files = []
    operations = []
    for key, role in (
        [
            (row.file_storage_key, "preview_source"),
            (row.failed_rows_file, "import_errors"),
        ]
        if batch
        else [(row.file_storage_key, "export")]
    ):
        if not key:
            continue
        if not file_storage.is_owned_artifact(key):
            # Old random keys lack a publication-owner/tombstone proof. Their
            # private bytes stay for maintenance instead of blind deletion.
            return None
        try:
            owner = await storage.artifact_identity(key)
        except (BusinessRuleException, FileNotFoundError):
            return None
        if (
            owner.tenant_id != row.tenant_id
            or owner.resource_id != resource_id
            or owner.role != role
        ):
            return None
        files.append((key, owner))
        operations.append(owner.operation_id)
    return CleanupIntent(
        TransferResource(
            row.tenant_id,
            row.operator_id,
            kind,
            resource_id,
            tuple(sorted(set(operations))),
        ),
        storage,
        tuple(files),
        row.preview_token if batch else None,
    )


async def guard_cleanup_rows(db, rows, *, storage=None):
    from app.modules.ai.service.file_cleanup_dependency_service import (  # noqa: PLC0415
        lock_transfer_dependencies,
    )

    intentions = [await cleanup_resource(row, storage=storage) for row in rows]
    allowed = await lock_transfer_dependencies(
        db,
        [intent.resource for intent in intentions if intent is not None],
    )
    return {
        intent.resource.resource_id: intent
        for intent in intentions
        if intent is not None and allowed[intent.resource]
    }


async def lock_cleanup_row(db, row, intent):
    model = type(row)
    column = model.batch_id if isinstance(row, UserImportBatch) else model.export_id
    current = await db.scalar(
        select(model)
        .where(
            model.tenant_id == intent.resource.tenant_id,
            column == intent.resource.resource_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        current is None
        or await cleanup_resource(current, storage=intent.storage) != intent
    ):
        return None
    return current


def stage_cleanup(db, intent):
    tx = db.get_transaction().sync_transaction
    old_tx, queue = db.info.get(_QUEUE, (None, []))
    if old_tx is not tx:
        queue = []
    db.info[_QUEUE] = (tx, [*queue, intent])


def take_cleanup_intentions(db):
    """Capture immediately before the owner's commit; rollback cannot reuse it."""
    old_tx, queue = db.info.pop(_QUEUE, (None, []))
    current = db.get_transaction()
    return tuple(queue) if current and current.sync_transaction is old_tx else ()


async def finish_committed_cleanup(intentions):
    """Only call after commit returned; fresh locks exclude unresolved references.

    Failed/unknown deletion keeps private bytes. There is no detached writer or
    background retry. Manifest retirement atomically excludes late publication.
    """
    from app.modules.ai.agents.gateway.atomic_execution import (  # noqa: PLC0415
        _owned_session,
    )
    from app.modules.ai.service.file_cleanup_dependency_service import (  # noqa: PLC0415
        lock_transfer_dependencies,
    )

    for intent in intentions:
        resource = intent.resource
        try:
            async with asyncio.timeout(8), _owned_session() as db:
                await db.begin()
                allowed = await lock_transfer_dependencies(db, [resource])
                if not allowed[resource]:
                    continue
                is_batch = resource.kind == "user_import_batch"
                model = UserImportBatch if is_batch else UserExportTask
                column = model.batch_id if is_batch else model.export_id
                row = await db.scalar(
                    select(model)
                    .where(
                        model.tenant_id == resource.tenant_id,
                        column == resource.resource_id,
                    )
                    .with_for_update()
                )
                if row is not None and (
                    not is_batch
                    or row.operator_id != resource.operator_id
                    or row.status
                    not in {ImportBatchStatus.CANCELLED, ImportBatchStatus.EXPIRED}
                ):
                    continue
                for key, owner in intent.files:
                    await intent.storage.delete_artifact(key, identity=owner)
                if intent.preview_token:
                    await redis_module.redis_client.delete(
                        f"tenant:{resource.tenant_id}:user_import:preview:{intent.preview_token}"
                    )
        except (Exception, asyncio.CancelledError):
            logger.warning(
                "committed transfer cleanup retained private artifact", exc_info=True
            )
