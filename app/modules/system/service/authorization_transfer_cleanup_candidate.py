"""Two-owner retention: commit expiry, then prove and retire exact artifacts."""

from dataclasses import dataclass
from datetime import UTC, timedelta

from sqlalchemy import select

from app.core.authorization_plan import LockPlan, ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.db.authorization_plan_session import _order_error
from app.modules.ai.service.authorization_context_candidate import ai_context_candidate
from app.modules.ai.service.file_cleanup_dependency_service import (
    TransferResource,
    scan_transfer_dependencies,
    transfer_bindings,
    validate_transfer_dependencies,
)
from app.modules.system.service.authorization_resource_catalog import resource_spec


@dataclass(frozen=True, slots=True, repr=False)
class CleanupInput:
    resource: TransferResource
    key: ResourceKey
    files: tuple
    snapshot: dict


class TransferCleanupCandidate:
    async def prepare(
        self, db, *, tenant_id, kind, identifier, storage, inspect_artifacts=True
    ):
        if kind not in {"export", "import"}:
            raise _order_error()
        key = ResourceKey(
            K.EXPORT_TASK if kind == "export" else K.IMPORT_BATCH, tenant_id, identifier
        )
        row = await self._row(db, key)
        if row is None:
            raise _order_error()
        files = []
        operation_ids = []
        pairs = (
            ((row["file_storage_key"], "export"),)
            if kind == "export"
            else (
                (row["file_storage_key"], "preview_source"),
                (row["failed_rows_file"], "import_errors"),
            )
        )
        for path, role in pairs:
            if path is None or not inspect_artifacts:
                continue
            owner = await storage.artifact_identity(path)
            if (
                owner.tenant_id != tenant_id
                or owner.resource_id != identifier
                or owner.role != role
            ):
                raise _order_error()
            files.append((path, owner))
            if owner.operation_id not in {
                "rest-export:" + identifier,
                "rest-preview:" + identifier,
            }:
                operation_ids.append(owner.operation_id)
        return CleanupInput(
            TransferResource(
                tenant_id,
                row["operator_id"],
                "user_export_task" if kind == "export" else "user_import_batch",
                identifier,
                tuple(sorted(set(operation_ids))),
            ),
            key,
            tuple(files),
            row,
        )

    async def _row(self, db, key):
        spec = resource_spec(key.kind)
        row = (
            (
                await db.execute(
                    select(spec.table).where(
                        spec.table.c.tenant_id == key.tenant_id,
                        spec.table.c[spec.key_columns[0]] == key.identity[0],
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) | {"status": row["status"].value} if row else None

    async def execute(self, db, tx, command, *, storage, retire):
        tx.assert_session(db)
        if type(command) is not CleanupInput or type(retire) is not bool:
            raise _order_error()
        resource = command.resource
        actions, logs = await scan_transfer_dependencies(db, (resource,))
        before = transfer_bindings(actions, logs)
        inventory = await ai_context_candidate.discover(
            db,
            tx,
            tenant_id=resource.tenant_id,
            conversation_ids=tuple(
                sorted({r.conversation_id for r in [*actions, *logs]})
            ),
        )
        await tx.lock(LockPlan(existing=(*inventory.keys, command.key)))
        await ai_context_candidate.recheck(db, tx, inventory)
        actions, logs = await scan_transfer_dependencies(db, (resource,))
        if (
            transfer_bindings(actions, logs) != before
            or not (
                await validate_transfer_dependencies(db, (resource,), actions, logs)
            )[resource]
        ):
            return "retained"
        current = await self._row(db, command.key)
        if current != command.snapshot:
            return "retained"
        created = current["created_at"]
        created = created.replace(tzinfo=UTC) if created.tzinfo is None else created
        now = await tx.database_now()
        ttl = (
            timedelta(minutes=10)
            if command.key.kind is K.IMPORT_BATCH
            and current["status"] in {"CREATED", "PREVIEW_DONE"}
            else timedelta(days=30)
        )
        if current["status"] == "RUNNING" or (
            current["status"] != "EXPIRED" and now - created < ttl
        ):
            return "retained"
        if not retire:
            await tx.update_factual(
                command.key, {"status": "EXPIRED"}, expected=current
            )
            return "expired"
        if current["status"] != "EXPIRED":
            raise _order_error()
        tx.seal_insertions()
        with tx.file_io(max_reads=4, max_total_bytes=40 * 1024 * 1024):
            for path, owner in command.files:
                if await storage.artifact_identity(path) != owner:
                    raise _order_error()
            for path, owner in command.files:
                await storage.delete_artifact(path, identity=owner)
        changes = {"file_storage_key": None}
        if command.key.kind is K.IMPORT_BATCH:
            changes["failed_rows_file"] = None
        await tx.update_factual(command.key, changes, expected=current)
        return "retired"


transfer_cleanup_candidate = TransferCleanupCandidate()
