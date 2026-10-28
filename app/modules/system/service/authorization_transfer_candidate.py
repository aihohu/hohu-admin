"""Fixed transfer plans; local publication follows every unique/row wait."""

from dataclasses import asdict, dataclass, replace
from datetime import UTC, timedelta
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import select

from app.core.authorization_io import file_transform_worker
from app.core.authorization_plan import LockPlan, NewResource, ResourceKey
from app.core.authorization_plan import ResourceKind as K
from app.core.exceptions import AuthorizationException, BusinessRuleException
from app.core.file_storage import ArtifactExecution, is_owned_artifact
from app.core.id_generator import next_id
from app.core.rbac import is_super_admin
from app.db.authorization_plan_session import _order_error
from app.modules.ai.models.conversation import AiConversation
from app.modules.ai.service.authorization_context_candidate import ai_context_candidate
from app.modules.ai.service.file_cleanup_dependency_service import (
    TransferResource,
    scan_transfer_dependencies,
    validate_transfer_dependencies,
)
from app.modules.ai.service.result_projection_service import result_projection_service
from app.modules.auth.service import load_live_user_authority
from app.modules.system.ai_tools.user_transfer import (
    _load_file_bytes,
    _user_import_mime_for_filename,
)
from app.modules.system.constants import ExportTaskStatus, ImportBatchStatus
from app.modules.system.models.user_transfer import UserImportBatch
from app.modules.system.schemas.user_transfer import (
    ImportDryRunResult,
    UserExportFilter,
    UserImportRecord,
)
from app.modules.system.service import user_export_service as exports
from app.modules.system.service import user_import_service as imports
from app.modules.system.service.authorization_control_identity import (
    assert_identity,
    read_identity,
)
from app.modules.system.service.authorization_resource_catalog import resource_spec
from app.modules.system.service.file_policy_service import file_policy_service
from app.modules.system.service.user_import_parser import (
    import_file_has_column,
    parse_import_excel,
)
from app.modules.system.service.user_import_validator import (
    _compute_accessible_dept_ids,
)


def _limit():
    return BusinessRuleException(
        "文件或行数超过同步预算", error_code="AUTHORIZATION_FILE_LIMIT_EXCEEDED"
    )


@dataclass(frozen=True, slots=True, repr=False)
class TransferInput:
    tenant: object
    version: int
    kind: str
    resource_id: str
    execution: ArtifactExecution
    values: tuple


@dataclass(slots=True, repr=False)
class _TransferWork:
    transaction: object
    command: TransferInput
    identity: object
    locks: LockPlan
    used: bool = False


@dataclass(frozen=True, slots=True, repr=False)
class TransferResult:
    resource_id: str
    count: int
    storage_key: str
    preview_token: str | None = None
    summary: object = None
    preview_data: dict | None = None
    data: bytes | None = None
    filename: str | None = None


class TransferCandidate:
    async def rest_download(
        self,
        db,
        tx,
        *,
        tenant,
        actor_user_version,
        identifier,
        storage,
        signed=False,
        token=None,
    ):
        """Administrative download, including existing private legacy exports."""
        tx.assert_session(db)
        if type(signed) is not bool:
            raise _order_error()
        if type(identifier) is not str or not 1 <= len(identifier) <= 64:
            raise exports.NotFoundException(
                "用户导出任务", error_code="AI_EXPORT_TASK_NOT_FOUND"
            )
        identity = await read_identity(
            db,
            tenant=tenant,
            actor_user_version=actor_user_version,
            permissions=("system:user:export",),
        )
        user = await load_live_user_authority(db, tenant=tenant)
        cross_owner = not signed and is_super_admin(user)
        task = await exports.get_export_task(
            db,
            identifier,
            operator_id=tenant.actor_user_id,
            allow_cross_owner=cross_owner,
            tenant=tenant,
        )
        if task is None:
            raise exports.NotFoundException(
                "用户导出任务", error_code="AI_EXPORT_TASK_NOT_FOUND"
            )
        key = ResourceKey(K.EXPORT_TASK, tenant.tenant_id, identifier)
        cids = tuple(
            await db.scalars(
                select(AiConversation.conversation_id).where(
                    AiConversation.tenant_id == tenant.tenant_id,
                    AiConversation.user_id == task.operator_id,
                )
            )
        )
        inventory = await ai_context_candidate.discover(
            db, tx, tenant_id=tenant.tenant_id, conversation_ids=cids
        )
        await tx.lock(
            LockPlan(existing=tuple(set(identity.keys) | set(inventory.keys) | {key}))
        )
        await assert_identity(db, identity)
        await ai_context_candidate.recheck(db, tx, inventory)
        user = await load_live_user_authority(db, tenant=tenant)
        await db.refresh(task)
        if task.tenant_id != tenant.tenant_id or (
            not cross_owner and task.operator_id != tenant.actor_user_id
        ):
            raise exports.NotFoundException(
                "用户导出任务", error_code="AI_EXPORT_TASK_NOT_FOUND"
            )
        if signed:
            lineage = (
                result_projection_service.read_download_token(
                    token,
                    user,
                    tenant=tenant,
                    resource_type="user_export",
                    resource_id=identifier,
                )
                if type(token) is str and 1 <= len(token) <= 16384
                else None
            )
            if not await result_projection_service.authorize_result_projection(
                db, user, owner_user_id=task.operator_id, lineage=lineage
            ):
                raise AuthorizationException(
                    error_code="AI_RESULT_PROJECTION_FORBIDDEN"
                )
        if task.status != ExportTaskStatus.SUCCESS:
            raise BusinessRuleException(
                "导出任务未成功，无法下载", error_code="AI_EXPORT_TASK_NOT_READY"
            )
        if not task.file_storage_key:
            raise BusinessRuleException(
                "导出文件缺失", error_code="AI_EXPORT_FILE_MISSING"
            )
        tx.seal_insertions()
        with tx.file_io(max_reads=2, max_total_bytes=20 * 1024 * 1024):
            operations = ()
            owner = None
            if task.file_storage_key and is_owned_artifact(task.file_storage_key):
                try:
                    owner = await storage.artifact_identity(task.file_storage_key)
                except FileNotFoundError as exc:
                    raise BusinessRuleException(
                        "导出文件已过期", error_code="AI_EXPORT_FILE_EXPIRED"
                    ) from exc
                if (owner.tenant_id, owner.resource_id, owner.role) != (
                    tenant.tenant_id,
                    identifier,
                    "export",
                ):
                    raise _order_error()
                if owner.operation_id != "rest-export:" + identifier:
                    operations = (owner.operation_id,)
            resource = TransferResource(
                tenant.tenant_id,
                task.operator_id,
                "user_export_task",
                identifier,
                operations,
            )
            actions, logs = await scan_transfer_dependencies(db, (resource,))
            if not (
                await validate_transfer_dependencies(db, (resource,), actions, logs)
            )[resource]:
                raise BusinessRuleException(
                    "导出文件关联执行待核对", error_code="AI_EXECUTION_OUTCOME_UNKNOWN"
                )
            try:
                data = (
                    await storage.read_artifact(task.file_storage_key, identity=owner)
                    if owner is not None
                    else await storage.read(task.file_storage_key)
                )
            except FileNotFoundError as exc:
                raise BusinessRuleException(
                    "导出文件已过期", error_code="AI_EXPORT_FILE_EXPIRED"
                ) from exc
            return data, f"hohu_users_{task.created_at.strftime('%Y%m%d_%H%M%S')}.xlsx"

    def prepare_preview(
        self,
        *,
        tenant,
        actor_user_version,
        file_bytes,
        filename,
        reason,
        on_conflict="skip",
        execution=None,
        mime_type=None,
    ):
        if (
            type(file_bytes) is not bytes
            or len(file_bytes) > 10 * 1024 * 1024
            or type(filename) is not str
            or not 1 <= len(filename) <= 256
            or on_conflict not in {"skip", "overwrite", "fail_fast"}
        ):
            raise _order_error()
        mime = (
            _user_import_mime_for_filename(filename) if mime_type is None else mime_type
        )
        records = parse_import_excel(file_bytes, mime)
        if not 1 <= len(records) <= 2000:
            raise _limit()
        role_column = import_file_has_column(file_bytes, mime, "role_input")
        identifier = imports._generate_batch_id()
        execution = execution or ArtifactExecution(
            tenant.tenant_id, "rest-preview:" + identifier, str(uuid4())
        )
        if (
            type(execution) is not ArtifactExecution
            or execution.tenant_id != tenant.tenant_id
        ):
            raise _order_error()
        execution.identity("preview_source", identifier)
        return TransferInput(
            tenant,
            actor_user_version,
            "preview",
            identifier,
            execution,
            (
                tuple(r.model_dump_json() for r in records),
                file_bytes,
                filename,
                imports._validate_reason(reason),
                on_conflict,
                role_column,
                str(uuid4()),
            ),
        )

    def prepare_export(self, *, tenant, actor_user_version, args, execution=None):
        if type(args) is not dict or set(args) - {
            "reason",
            "user_name",
            "user_names",
            "nickname",
            "user_email",
            "user_phone",
            "status",
            "dept_id",
        }:
            raise _order_error()
        reason = exports._validate_reason(args.get("reason"))
        filters = UserExportFilter.model_validate(
            {k: v for k, v in args.items() if k != "reason"}
        )
        identifier = str(next_id())
        execution = execution or ArtifactExecution(
            tenant.tenant_id, "rest-export:" + identifier, str(uuid4())
        )
        if (
            type(execution) is not ArtifactExecution
            or execution.tenant_id != tenant.tenant_id
        ):
            raise _order_error()
        execution.identity("export", identifier)
        return TransferInput(
            tenant,
            actor_user_version,
            "export",
            identifier,
            execution,
            (filters.model_dump_json(), reason),
        )

    async def plan(self, db, tx, command):
        tx.assert_session(db)
        if type(command) is not TransferInput or command.kind not in {
            "export",
            "preview",
        }:
            raise _order_error()
        tx.assert_planning(command.tenant.tenant_id, exclusive=True)
        permissions = (
            ("system:user:export",)
            if command.kind == "export"
            else ("system:user:import", "system:dept:list")
            + (("system:user:role-auth",) if command.values[5] else ())
        )
        identity = await read_identity(
            db,
            tenant=command.tenant,
            actor_user_version=command.version,
            permissions=permissions,
        )
        if command.kind == "preview":
            policy = await file_policy_service.resolve(
                db, "import", tenant=command.tenant
            )
            file_policy_service.validate_import(
                policy, command.values[2], len(command.values[1])
            )
        key = ResourceKey(
            K.EXPORT_TASK if command.kind == "export" else K.IMPORT_BATCH,
            command.tenant.tenant_id,
            command.resource_id,
        )
        return _TransferWork(
            tx,
            command,
            identity,
            LockPlan(existing=identity.keys, new=(NewResource(key),)),
        )

    async def apply(self, db, tx, work, *, storage):
        tx.assert_session(db)
        if type(work) is not _TransferWork or work.transaction is not tx or work.used:
            raise _order_error()
        work.used = True
        tx.assert_planned(work.locks)
        await assert_identity(db, work.identity)
        command = work.command
        tenant = command.tenant
        user = await load_live_user_authority(db, tenant=tenant)
        if command.kind == "preview":
            batch_view, detail = await self.materialize_preview(db, tx, command, user)
            result = await self.publish_preview(db, tx, command, storage, batch_view)
            data = detail.model_dump(mode="json", by_alias=True)
            data.update(
                newCount=batch_view["summary_new"],
                existsCount=batch_view["summary_exists"],
                conflictCount=batch_view["summary_conflict"],
                outOfScopeCount=batch_view["summary_out_of_scope"],
                previewToken=result.preview_token,
                expiresAt=(
                    batch_view["created_at"] + timedelta(minutes=10)
                ).isoformat(),
            )
            return replace(result, preview_data=data)
        filters = UserExportFilter.model_validate_json(command.values[0])
        rows = await exports._query_users_with_data_scope(
            db, filters, user, tenant=tenant, max_rows=5001
        )
        if len(rows) > 5000:
            raise exports.UnprocessableEntityException(
                "超过同步导出行数上限，请缩小筛选范围",
                error_code="AI_EXPORT_ASYNC_REQUIRED",
            )
        lookup = await exports._build_dept_lookup_for_rows(db, rows, tenant=tenant)
        values = exports._excel_values(rows, lookup)
        data = await file_transform_worker.run(exports._build_excel_values, values)
        if len(data) > 10 * 1024 * 1024:
            raise _limit()
        departments = await _compute_accessible_dept_ids(db, user, tenant=tenant)
        now = (await tx.database_now()).astimezone(UTC).replace(tzinfo=None)
        key = ResourceKey(K.EXPORT_TASK, tenant.tenant_id, command.resource_id)
        identity = command.execution.identity("export", command.resource_id)
        await tx.insert_reserved(
            key,
            {
                "operator_id": tenant.actor_user_id,
                "filter_snapshot": {
                    "filter": filters.model_dump(mode="json"),
                    "accessible_dept_ids": None
                    if departments is None
                    else sorted(departments),
                    "filter_evaluated_at": now.isoformat(),
                    "artifact": asdict(identity),
                    "data_scope_hash": await result_projection_service.compute_data_scope_hash(
                        db, user
                    ),
                },
                "reason": command.values[1],
                "status": "RUNNING",
                "started_at": now,
            },
        )
        tx.seal_insertions()
        with tx.file_io(max_reads=2, max_total_bytes=20 * 1024 * 1024):
            storage_key = await storage.save_artifact(data, identity=identity)
            if (
                storage_key != identity.storage_key
                or await storage.read_artifact(storage_key, identity=identity) != data
            ):
                raise _order_error()
        finished = (await tx.database_now()).astimezone(UTC).replace(tzinfo=None)
        await tx.update_factual(
            key,
            {
                "status": "SUCCESS",
                "file_storage_key": storage_key,
                "file_size_bytes": len(data),
                "row_count": len(rows),
                "finished_at": finished,
                "duration_ms": int((finished - now).total_seconds() * 1000),
            },
            expected={"status": "RUNNING", "operator_id": tenant.actor_user_id},
        )
        table = resource_spec(K.EXPORT_TASK).table
        created = await db.scalar(
            select(table.c.created_at).where(
                table.c.tenant_id == tenant.tenant_id,
                table.c.export_id == command.resource_id,
            )
        )
        return TransferResult(
            command.resource_id,
            len(rows),
            storage_key,
            data=data,
            filename=f"hohu_users_{created.strftime('%Y%m%d_%H%M%S')}.xlsx",
        )

    async def materialize_preview(self, db, tx, command, user):
        tx.assert_session(db)
        serialized, data, filename, reason, conflict, role_column, token = (
            command.values
        )
        records = [UserImportRecord.model_validate_json(r) for r in serialized]
        groups = await imports._classify_records(
            db,
            records,
            user,
            has_role_column=role_column,
            strict_authorization=True,
            tenant=command.tenant,
        )
        counts = dict(
            zip(
                ("new", "exists", "conflict", "out_of_scope"),
                (len(g) for g in groups),
                strict=True,
            )
        )
        key = ResourceKey(K.IMPORT_BATCH, command.tenant.tenant_id, command.resource_id)
        await tx.insert_reserved(
            key,
            {
                "operator_id": command.tenant.actor_user_id,
                "filename": filename,
                "file_sha256": sha256(data).hexdigest(),
                "records_hash": imports._compute_records_hash(records),
                "total_rows": len(records),
                "preview_token": token,
                "on_conflict": conflict,
                "reason": reason,
                "status": "CREATED",
            },
        )
        batch = await db.get(
            UserImportBatch, command.resource_id, populate_existing=True
        )
        batch_view = {
            c.name: getattr(batch, c.name) for c in UserImportBatch.__table__.c
        }
        batch_view.update(**{"summary_" + k: v for k, v in counts.items()})
        detail = ImportDryRunResult(
            total=len(records),
            new_records=groups[0],
            exists_records=groups[1],
            conflict_records=groups[2],
            out_of_scope_records=groups[3],
        )
        return batch_view, detail

    async def publish_preview(
        self,
        db,
        tx,
        command,
        storage,
        batch_view,
        *,
        source_context=None,
        source_file_id=None,
    ):
        tx.assert_session(db)
        data, token = command.values[1], command.values[6]
        key = ResourceKey(K.IMPORT_BATCH, command.tenant.tenant_id, command.resource_id)
        identity = command.execution.identity("preview_source", command.resource_id)
        counts = {
            k: batch_view["summary_" + k]
            for k in ("new", "exists", "conflict", "out_of_scope")
        }
        tx.seal_insertions()
        with tx.file_io(max_reads=3, max_total_bytes=30 * 1024 * 1024):
            if source_context is not None:
                original, _, _ = await _load_file_bytes(source_context, source_file_id)
                if original != data:
                    raise _order_error()
            storage_key = await storage.save_artifact(data, identity=identity)
            if (
                storage_key != identity.storage_key
                or await storage.read_artifact(storage_key, identity=identity) != data
            ):
                raise _order_error()
        await tx.update_factual(
            key,
            {
                "status": "PREVIEW_DONE",
                "file_storage_key": storage_key,
                **{"summary_" + k: v for k, v in counts.items()},
            },
            expected={"status": "CREATED", "operator_id": command.tenant.actor_user_id},
        )
        return TransferResult(
            command.resource_id, batch_view["total_rows"], storage_key, token, counts
        )

    async def download(
        self,
        db,
        tx,
        *,
        tenant,
        actor_user_version,
        kind,
        identifier,
        storage,
        token=None,
    ):
        tx.assert_session(db)
        if (
            kind not in {"export", "import_errors", "preview_source"}
            or type(identifier) is not str
        ):
            raise _order_error()
        export = kind == "export"
        key = ResourceKey(
            K.EXPORT_TASK if export else K.IMPORT_BATCH, tenant.tenant_id, identifier
        )
        identity = await read_identity(
            db,
            tenant=tenant,
            actor_user_version=actor_user_version,
            permissions=("system:user:export" if export else "system:user:import",),
        )
        cids = tuple(
            await db.scalars(
                select(AiConversation.conversation_id).where(
                    AiConversation.tenant_id == tenant.tenant_id,
                    AiConversation.user_id == tenant.actor_user_id,
                )
            )
        )
        inventory = await ai_context_candidate.discover(
            db, tx, tenant_id=tenant.tenant_id, conversation_ids=cids
        )
        await tx.lock(
            LockPlan(existing=tuple(set(identity.keys) | set(inventory.keys) | {key}))
        )
        await ai_context_candidate.recheck(db, tx, inventory)
        await assert_identity(db, identity)
        table = resource_spec(key.kind).table
        row = (
            (
                await db.execute(
                    select(table).where(
                        table.c.tenant_id == tenant.tenant_id,
                        table.c[resource_spec(key.kind).key_columns[0]] == identifier,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None or row["operator_id"] != tenant.actor_user_id:
            raise AuthorizationException(error_code="AI_RESULT_PROJECTION_FORBIDDEN")
        now = await tx.database_now()
        created = row["created_at"]
        created = created.replace(tzinfo=UTC) if created.tzinfo is None else created
        valid = (
            row["status"] == ExportTaskStatus.SUCCESS
            if export
            else row["status"]
            in {
                ImportBatchStatus.SUCCESS,
                ImportBatchStatus.PARTIAL_SUCCESS,
                ImportBatchStatus.FAILED,
                ImportBatchStatus.CANCELLED,
            }
            if kind == "import_errors"
            else row["status"] == ImportBatchStatus.PREVIEW_DONE
        )
        if not valid or now - created > (
            timedelta(minutes=10) if kind == "preview_source" else timedelta(days=30)
        ):
            raise _order_error()
        user = await load_live_user_authority(db, tenant=tenant)
        if export:
            if row["filter_snapshot"].get(
                "data_scope_hash"
            ) != await result_projection_service.compute_data_scope_hash(db, user):
                raise AuthorizationException(
                    error_code="AI_RESULT_PROJECTION_FORBIDDEN"
                )
            current = await _compute_accessible_dept_ids(db, user, tenant=tenant)
            previous = row["filter_snapshot"].get("accessible_dept_ids")
            if current is not None and (
                previous is None or not set(previous) <= set(current)
            ):
                raise AuthorizationException(
                    error_code="AI_RESULT_PROJECTION_FORBIDDEN"
                )
        storage_key = row[
            "failed_rows_file" if kind == "import_errors" else "file_storage_key"
        ]
        if not storage_key:
            raise _order_error()
        tx.seal_insertions()
        with tx.file_io(max_reads=2, max_total_bytes=20 * 1024 * 1024):
            owner = await storage.artifact_identity(storage_key)
            if (
                owner.tenant_id != tenant.tenant_id
                or owner.resource_id != identifier
                or owner.role != kind
                or (export and asdict(owner) != row["filter_snapshot"].get("artifact"))
            ):
                raise _order_error()
            if owner.operation_id not in {
                "rest-export:" + identifier,
                "rest-preview:" + identifier,
                "rest-import:" + identifier,
            }:
                resource = TransferResource(
                    tenant.tenant_id,
                    tenant.actor_user_id,
                    "user_export_task" if export else "user_import_batch",
                    identifier,
                    (owner.operation_id,),
                )
                actions, logs = await scan_transfer_dependencies(db, (resource,))
                if not (
                    await validate_transfer_dependencies(db, (resource,), actions, logs)
                )[resource]:
                    raise _order_error()
                if export:
                    lineage = (
                        result_projection_service.read_download_token(
                            token,
                            user,
                            tenant=tenant,
                            resource_type="user_export",
                            resource_id=identifier,
                        )
                        if type(token) is str
                        else None
                    )
                    if not await result_projection_service.authorize_result_projection(
                        db, user, owner_user_id=tenant.actor_user_id, lineage=lineage
                    ):
                        raise AuthorizationException(
                            error_code="AI_RESULT_PROJECTION_FORBIDDEN"
                        )
            data = await storage.read_artifact(storage_key, identity=owner)
            if (export and len(data) != row["file_size_bytes"]) or (
                kind == "preview_source"
                and sha256(data).hexdigest() != row["file_sha256"]
            ):
                raise _order_error()
        return data, f"hohu_users_{created.strftime('%Y%m%d_%H%M%S')}.xlsx"


transfer_candidate = TransferCandidate()
