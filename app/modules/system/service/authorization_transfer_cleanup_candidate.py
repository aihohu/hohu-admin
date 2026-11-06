"""Two-owner retention: commit expiry, then prove and retire exact artifacts."""

from dataclasses import dataclass
from datetime import UTC, timedelta

from sqlalchemy import select

from app.core.authorization_plan import LockPlan, NewResource, ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.core.exceptions import (
    AuthorizationException,
    BusinessRuleException,
    UnprocessableEntityException,
)
from app.core.id_generator import next_id
from app.core.rbac import is_super_admin
from app.db.authorization_plan_session import _order_error
from app.modules.ai.service.authorization_context_candidate import ai_context_candidate
from app.modules.ai.service.file_cleanup_dependency_service import (
    TransferResource,
    scan_transfer_dependencies,
    transfer_bindings,
    validate_transfer_dependencies,
)
from app.modules.auth.service import load_live_user_authority
from app.modules.system.schemas.user_transfer import UserImportBatchCancelResponse
from app.modules.system.service.authorization_control_identity import (
    assert_identity,
    read_identity,
)
from app.modules.system.service.authorization_resource_catalog import resource_spec
from app.modules.system.service.user_import_service import _validate_reason


@dataclass(frozen=True, slots=True, repr=False)
class CleanupInput:
    resource: TransferResource
    key: ResourceKey
    files: tuple
    snapshot: dict


class TransferCleanupCandidate:
    async def cancel(self, db, tx, command, *, tenant, actor_user_version, reason):
        tx.assert_session(db)
        tx.assert_planning(tenant.tenant_id, exclusive=True)
        reason = _validate_reason(reason)
        if (
            type(command) is not CleanupInput
            or command.key.kind is not K.IMPORT_BATCH
            or command.key.tenant_id != tenant.tenant_id
        ):
            raise _order_error()
        identity = await read_identity(
            db,
            tenant=tenant,
            actor_user_version=actor_user_version,
            permissions=("system:user:import",),
        )
        user = await load_live_user_authority(db, tenant=tenant)
        if command.resource.operator_id != tenant.actor_user_id and not is_super_admin(
            user
        ):
            raise AuthorizationException("无权取消此批次")
        resource = command.resource
        actions, logs = await scan_transfer_dependencies(db, (resource,))
        before = transfer_bindings(actions, logs)
        inventory = await ai_context_candidate.discover(
            db,
            tx,
            tenant_id=tenant.tenant_id,
            conversation_ids=tuple(
                sorted({row.conversation_id for row in [*actions, *logs]})
            ),
        )
        log = ResourceKey(K.IMPORT_BATCH_LOG, tenant.tenant_id, str(next_id()))
        await tx.lock(
            LockPlan(
                existing=tuple(
                    set(identity.keys) | set(inventory.keys) | {command.key}
                ),
                new=(NewResource(log),),
            )
        )
        await assert_identity(db, identity)
        await ai_context_candidate.recheck(db, tx, inventory)
        actions, logs = await scan_transfer_dependencies(db, (resource,))
        current = await self._row(db, command.key)
        if (
            transfer_bindings(actions, logs) != before
            or current != command.snapshot
            or not (
                await validate_transfer_dependencies(db, (resource,), actions, logs)
            )[resource]
        ):
            raise BusinessRuleException(
                "批次关联操作或文件归属待核对，暂不能取消",
                error_code="AI_EXECUTION_OUTCOME_UNKNOWN",
            )
        status = current["status"]
        if status not in {"PREVIEW_DONE", "RUNNING"}:
            raise UnprocessableEntityException(
                "批次状态不可取消", error_code="AI_IMPORT_BATCH_NOT_CANCELLABLE"
            )
        now = await tx.database_now()
        if status == "PREVIEW_DONE":
            await tx.update_factual(
                command.key,
                {"status": "CANCELLED", "finished_at": now},
                expected=current,
            )
            await tx.insert_reserved(
                log,
                {
                    "batch_id": resource.resource_id,
                    "operator_id": tenant.actor_user_id,
                    "event": "CANCELLED",
                    "from_status": "PREVIEW_DONE",
                    "to_status": "CANCELLED",
                    "detail": {
                        "reason": reason,
                        "cancelled_by": tenant.actor_user_id,
                        "scenario": "preview_done_direct_cancel",
                    },
                },
            )
            status = "CANCELLED"
        return UserImportBatchCancelResponse(
            batch_id=resource.resource_id, status=status, cancelled_at=now
        )

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
                "rest-import:" + identifier,
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
        expiry_log = (
            ResourceKey(K.IMPORT_BATCH_LOG, resource.tenant_id, str(next_id()))
            if not retire
            and command.key.kind is K.IMPORT_BATCH
            and command.snapshot["status"] == "PREVIEW_DONE"
            else None
        )
        await tx.lock(
            LockPlan(
                existing=(*inventory.keys, command.key),
                new=(NewResource(expiry_log),) if expiry_log is not None else (),
            )
        )
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
            and current["status"] == "PREVIEW_DONE"
            else timedelta(days=90)
            if command.key.kind is K.IMPORT_BATCH
            else timedelta(days=30)
        )
        cancelled_retirement = retire and current["status"] == "CANCELLED"
        if current["status"] in {"CREATED", "RUNNING"} or (
            not cancelled_retirement
            and current["status"] != "EXPIRED"
            and now - created < ttl
        ):
            return "retained"
        if not retire:
            if current["status"] == "EXPIRED":
                return "expired"
            changes = {"status": "EXPIRED"}
            if expiry_log is not None:
                changes["finished_at"] = now
            await tx.update_factual(command.key, changes, expected=current)
            if expiry_log is not None:
                await tx.insert_reserved(
                    expiry_log,
                    {
                        "batch_id": resource.resource_id,
                        "operator_id": resource.operator_id,
                        "event": "EXPIRED",
                        "from_status": "PREVIEW_DONE",
                        "to_status": "EXPIRED",
                        "detail": {
                            "reason": "preview TTL 10min expired (cleanup cron)"
                        },
                    },
                )
            return "expired"
        if current["status"] not in {"EXPIRED", "CANCELLED"}:
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
