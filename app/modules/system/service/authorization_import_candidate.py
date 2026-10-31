"""Explicit import candidate: one complete plan, row savepoints and one epoch.

The owner constructs ArtifactExecution and commits or rolls back. This service
never attaches the candidate protocol to legacy HTTP or AI execution owners.
"""

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.constants import ADMIN_USERNAME
from app.core.authorization_io import hash_password
from app.core.authorization_plan import (
    LockMode,
    LockPlan,
    NewResource,
    ResourceKind,
    TenantLock,
    TransactionBudget,
)
from app.core.exceptions import (
    AuthorizationException,
    BusinessRuleException,
    UnprocessableEntityException,
)
from app.core.file_storage import ArtifactExecution
from app.core.id_generator import next_id
from app.core.tenant import TenantContext
from app.db.authorization_owner import rollback_authorization_session
from app.db.authorization_plan_session import AuthorizationPlanSession, _order_error
from app.modules.system.constants import (
    FAILED_ROWS_PREVIEW_LIMIT,
    OVERWRITE_ALLOWED,
    RECOVERABLE_ERROR_CODES,
    USER_IMPORT_CHUNK_SIZE,
    USER_IMPORT_MAX_ROWS,
    EmployeeNoSyncMode,
    ImportBatchStatus,
)
from app.modules.system.field_policy.transfer import (
    make_lineage,
    safe_failed_rows,
    transfer_denied,
    validate_lineage,
)
from app.modules.system.models.setting import SystemSetting
from app.modules.system.models.user_transfer import UserImportBatch
from app.modules.system.schemas.user_transfer import (
    FailedRow,
    ImportResult,
    UserImportRecord,
)
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_resource_catalog import resource_spec
from app.modules.system.service.authorization_system_candidate import (
    _key,
    _limit,
)
from app.modules.system.service.authorization_system_candidate import (
    system_authorization_candidate as system,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)
from app.modules.system.service.file_policy_service import file_policy_service
from app.modules.system.service.grant_authority import grant_authority_service
from app.modules.system.service.user_import_parser import _validate_row
from app.modules.system.service.user_import_service import (
    _classify_records,
    _compute_file_sha256,
    _compute_records_hash,
    _determine_end_status,
    _extract_error_code,
    _failed_rows_to_xlsx_bytes,
    _handle_idempotent_replay,
    _make_failed_row_from_exc,
    _resolve_import_authorization_targets,
    _validate_reason,
)
from app.modules.system.service.user_import_state import validate_reason_consistency
from app.modules.system.service.user_import_validator import (
    SyncAction,
    classify_sync_action,
)
from app.modules.system.service.user_service import (
    DEFAULT_PASSWORD_CONFIG_KEY,
    get_default_password,
)
from app.modules.system.service.user_transfer_fields import user_transfer_fields

IMPORT_CANDIDATE_ENTRIES = MappingProxyType(
    {
        "user.import": {
            "source": "user_import_service.batch_create_users_from_records",
            "global_mode": "shared",
            "tenant_mode": "exclusive",
            "io_profile": "bulk_60s",
            "epoch_owner": "import_aggregate",
            "production_connected": False,
        },
    }
)
_MAX_BYTES = 10 * 1024 * 1024


def _stale():
    return BusinessRuleException(
        "授权事实已变化，请重新预览", error_code="AUTHORIZATION_SNAPSHOT_STALE"
    )


def _aborted(record):
    return FailedRow(
        row_num=record.row_num,
        field="_batch",
        value="",
        reason="严格冲突策略已回滚整批，本行未写入",
        error_code="AI_IMPORT_BATCH_ABORTED",
    )


class _StrictAbort(Exception):
    def __init__(self, failed):
        self.failed = failed


def _recoverable(exc):
    if isinstance(exc, IntegrityError):
        return (
            getattr(exc.orig, "sqlstate", None) == "23505"
            and _extract_error_code(exc) in RECOVERABLE_ERROR_CODES
        )
    return isinstance(exc, BusinessRuleException) and exc.error_code in (
        RECOVERABLE_ERROR_CODES - {"BusinessRuleException"}
    ) | {"AI_IMPORT_ADMIN_RENAME_FORBIDDEN"}


@dataclass(frozen=True, slots=True, repr=False)
class _ImportInput:
    values: tuple
    source_proof: tuple | None = None
    upload_size: int = 0


@dataclass(slots=True, repr=False)
class _ImportWork:
    transaction: object
    command: _ImportInput
    locks: LockPlan
    values: tuple
    used: bool = False


class ImportAuthorizationCandidate:
    @staticmethod
    def _input(
        db,
        tenant,
        records,
        file_bytes,
        preview_token,
        filename,
        on_conflict,
        sync_mode,
        has_role_column,
        artifact_execution,
    ):
        if (
            not isinstance(db, AuthorizationPlanSession)
            or type(tenant) is not TenantContext
            or type(artifact_execution) is not ArtifactExecution
            or artifact_execution.tenant_id != tenant.tenant_id
            or type(has_role_column) is not bool
            or type(sync_mode) is not EmployeeNoSyncMode
            or type(on_conflict) is not str
            or on_conflict not in {"skip", "overwrite", "fail_fast"}
            or type(preview_token) is not str
            or not 1 <= len(preview_token) <= 64
            or type(filename) is not str
            or not 1 <= len(filename) <= 256
        ):
            raise _order_error()
        journal = db._authorization_journal
        if (
            db.in_transaction()
            or db.new
            or db.dirty
            or db.deleted
            or journal.phase != "idle"
            or journal.unsafe
            or not journal.valid_bind
        ):
            raise _order_error()
        artifact_execution.identity("import_errors", "validate")
        if (
            type(records) is not list
            or not 1 <= len(records) <= USER_IMPORT_MAX_ROWS
            or type(file_bytes) is not bytes
            or len(file_bytes) > _MAX_BYTES
        ):
            raise _limit()
        if any(type(row) is not UserImportRecord for row in records):
            raise _order_error()
        # Freeze caller-owned mutable models before the first await.
        try:
            records = [
                UserImportRecord.model_validate(row.model_dump(exclude_unset=True))
                for row in records
            ]
        except ValidationError as exc:
            raise UnprocessableEntityException(
                "导入记录格式无效", error_code="VALIDATION_ERROR"
            ) from exc
        if len({row.row_num for row in records}) != len(records) or any(
            row.row_num < 1 for row in records
        ):
            raise _order_error()
        if sum(len(row.model_dump_json().encode()) for row in records) > _MAX_BYTES:
            raise _limit()
        for row in records:
            role_input = row.role_input or ""
            if (
                len(role_input.encode()) > 65536
                or len(role_input.split(",")) > 256
                or len(row.dept_input.encode()) > 8192
                or len(row.dept_input.split("/")) > 256
            ):
                raise _limit()
        return records

    @staticmethod
    async def _setting(db, tenant):
        row = (
            (
                await db.execute(
                    select(SystemSetting.__table__).where(
                        SystemSetting.tenant_id == tenant.tenant_id,
                        SystemSetting.setting_key == DEFAULT_PASSWORD_CONFIG_KEY,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None

    @staticmethod
    async def _batch(db, tenant, preview_token):
        row = (
            (
                await db.execute(
                    select(UserImportBatch.__table__).where(
                        UserImportBatch.tenant_id == tenant.tenant_id,
                        UserImportBatch.preview_token == preview_token,
                    )
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise UnprocessableEntityException(
                "preview_token 无效或已过期", error_code="AI_IMPORT_PREVIEW_INVALID"
            )
        return dict(row)

    async def _prepare_password(self, db, tenant, preview_token):
        failure = None
        try:
            async with asyncio.timeout(5):
                before = await self._batch(db, tenant, preview_token)
                password_error, password, setting = None, None, None
                if before["status"] == ImportBatchStatus.PREVIEW_DONE:
                    setting = await self._setting(db, tenant)
                    try:
                        password = await get_default_password(db, tenant=tenant)
                    except BusinessRuleException as exc:
                        if exc.error_code not in {
                            "AI_IMPORT_DEFAULT_PASSWORD_NOT_SET",
                            "AI_IMPORT_DEFAULT_PASSWORD_INVALID",
                        }:
                            raise
                        password_error = exc
                    else:
                        if (
                            setting is None
                            or setting["setting_value"] != password
                            or setting["status"] != "1"
                        ):
                            raise _stale()
                return password, password_error, setting
        except BaseException as exc:
            failure = exc
            raise
        finally:
            try:
                await rollback_authorization_session(db)
            except BaseException:
                if isinstance(failure, asyncio.CancelledError):
                    raise failure
                raise

    async def prepare_input(
        self,
        db,
        *,
        tenant,
        actor_user_version,
        records,
        file_bytes,
        preview_token,
        filename,
        reason,
        on_conflict="skip",
        sync_mode=EmployeeNoSyncMode.CREATE_ONLY,
        has_role_column=False,
        file_storage,
        artifact_execution,
    ):
        records = self._input(
            db,
            tenant,
            records,
            file_bytes,
            preview_token,
            filename,
            on_conflict,
            sync_mode,
            has_role_column,
            artifact_execution,
        )
        reason = _validate_reason(reason)
        file_hash, record_hash = (
            _compute_file_sha256(file_bytes),
            _compute_records_hash(records),
        )
        canonical = []
        for row in records:
            parsed, errors = _validate_row(
                row.row_num, row.model_dump(exclude_unset=True)
            )
            if errors:
                raise UnprocessableEntityException(
                    errors[0].reason, error_code=errors[0].error_code
                )
            canonical.append(parsed)
        records = canonical
        with bounded_authorization_reads(db):
            password, password_error, setting = await self._prepare_password(
                db, tenant, preview_token
            )
            password_hash = (
                await hash_password(password) if password is not None else None
            )
        return _ImportInput(
            (
                tenant,
                actor_user_version,
                records,
                preview_token,
                filename,
                reason,
                on_conflict,
                sync_mode,
                has_role_column,
                file_storage,
                artifact_execution,
                file_hash,
                record_hash,
                password_error,
                setting,
                password_hash,
            ),
            upload_size=len(file_bytes),
        )

    async def plan(self, db, tx, command):
        tx.assert_session(db)
        if type(command) is not _ImportInput:
            raise _order_error()
        (
            tenant,
            actor_user_version,
            records,
            preview_token,
            filename,
            reason,
            on_conflict,
            sync_mode,
            has_role_column,
            file_storage,
            artifact_execution,
            file_hash,
            record_hash,
            password_error,
            setting,
            password_hash,
        ) = command.values
        tid = tenant.tenant_id
        tx.assert_planning(tid, exclusive=True)
        await system._identity(db, tenant, actor_user_version)
        policy = await file_policy_service.resolve(db, "import", tenant=tenant)
        file_policy_service.validate_import(policy, filename, command.upload_size)
        authority = await grant_authority_service.build(
            db, tenant.actor_user_id, tenant=tenant
        )
        required = {"system:user:import", "system:dept:list"}
        if has_role_column or any(row.role_input for row in records):
            required.add("system:user:role-auth")
        if not required <= authority.permission_codes:
            raise AuthorizationException(
                "缺少导入权限", error_code="MISSING_PERMISSION"
            )
        batch = await self._batch(db, tenant, preview_token)
        validate_reason_consistency(batch["reason"], reason)
        if (
            batch["operator_id"] != tenant.actor_user_id
            or batch["file_sha256"] != file_hash
            or batch["records_hash"] != record_hash
            or batch["total_rows"] != len(records)
        ):
            raise BusinessRuleException(
                "preview_token 校验失败", error_code="AI_IMPORT_PREVIEW_INVALID"
            )
        batch_key = _key(ResourceKind.IMPORT_BATCH, tid, batch["batch_id"])
        context = await user_transfer_fields.import_context(
            db, tx, tenant=tenant, version=actor_user_version, records=records
        )
        if (
            batch["status"] == ImportBatchStatus.PREVIEW_DONE
            and batch["field_lineage"] is not None
        ):
            document = batch["field_lineage"].get("preview_source")
            validate_lineage(
                document,
                tenant_id=tid,
                owner_id=tenant.actor_user_id,
                resource_id=batch["batch_id"],
                kind="preview_source",
                stamp=await user_transfer_fields.stamp(db, tx, tenant_id=tid),
            )
            if (
                document["mode"] != sync_mode.value
                or document["sha256"] != file_hash
                or document["presence"]
                != [sorted(r.model_fields_set - {"row_num"}) for r in records]
                or batch["on_conflict"] != on_conflict
            ):
                raise transfer_denied()
        facts = await system._facts(db, tid)
        stable_ids = {}
        resolutions = (
            await _resolve_import_authorization_targets(
                db,
                records,
                has_role_column=has_role_column,
                prospective_user_ids=stable_ids,
                strict_authorization=True,
                tenant=tenant,
            )
            if batch["status"] == ImportBatchStatus.PREVIEW_DONE
            else []
        )
        if resolutions:
            await user_transfer_fields.import_rows(
                db,
                tx,
                tenant=tenant,
                version=actor_user_version,
                context=context,
                records=records,
                resolutions=resolutions,
                sync_mode=sync_mode.value,
            )
        existing = {
            _key(
                kind,
                tid,
                *(row[name] for name in resource_spec(kind).key_columns),
            )
            for kind, rows in facts.items()
            for row in rows
        } | {batch_key}
        new = set()
        for resolution in resolutions:
            uid = resolution.target_user_id or resolution.prospective_user_id
            if resolution.prospective_user_id is not None:
                new.add(_key(ResourceKind.USER, tid, uid))
            if resolution.dept_id is not None:
                new.add(_key(ResourceKind.USER_DEPTS, tid, uid, resolution.dept_id))
            for rid in resolution.role_ids or ():
                new.add(_key(ResourceKind.USER_ROLES, tid, uid, rid))
        # Worst-case logs are reserved before the RUNNING transition.
        logs = [
            _key(ResourceKind.IMPORT_BATCH_LOG, tid, str(next_id()))
            for _ in range(
                2
                + (len(records) + USER_IMPORT_CHUNK_SIZE - 1) // USER_IMPORT_CHUNK_SIZE
            )
        ]
        new.update(logs)
        return _ImportWork(
            tx,
            command,
            LockPlan(
                existing=tuple(existing),
                new=tuple(NewResource(key) for key in new - existing),
            ),
            (batch, batch_key, facts, stable_ids, resolutions, logs),
        )

    async def apply(self, db, tx, work):
        tx.assert_session(db)
        if type(work) is not _ImportWork or work.transaction is not tx or work.used:
            raise _order_error()
        work.used = True
        tx.assert_planned(work.locks)
        (
            tenant,
            actor_user_version,
            records,
            preview_token,
            filename,
            reason,
            on_conflict,
            sync_mode,
            has_role_column,
            file_storage,
            artifact_execution,
            file_hash,
            record_hash,
            password_error,
            setting,
            password_hash,
        ) = work.command.values
        tid = tenant.tenant_id
        batch, batch_key, facts, stable_ids, resolutions, logs = work.values
        actor = await system._identity(db, tenant, actor_user_version)
        if facts != await system._facts(db, tid) or batch != await self._batch(
            db, tenant, preview_token
        ):
            raise _stale()
        if batch["status"] != ImportBatchStatus.PREVIEW_DONE:
            return await _handle_idempotent_replay(db, batch["batch_id"], tenant=tenant)
        if setting != await self._setting(db, tenant):
            raise _stale()
        current = await _resolve_import_authorization_targets(
            db,
            records,
            has_role_column=has_role_column,
            prospective_user_ids=stable_ids,
            strict_authorization=True,
            tenant=tenant,
        )
        if resolutions != current:
            raise _stale()
        classified = await _classify_records(
            db,
            records,
            actor,
            has_role_column=has_role_column,
            resolutions=resolutions,
            strict_authorization=True,
            tenant=tenant,
        )
        tx.assert_held(batch_key)
        source_proof = work.command.source_proof
        work, failed, skipped = self._work(
            records, resolutions, classified, on_conflict, sync_mode
        )
        if any(kind == "create" for kind, _, _ in work):
            if password_error is not None:
                raise password_error
            if password_hash is None:
                raise _stale()
        if artifact_execution.operation_id == "rest-import":
            artifact_execution = ArtifactExecution(
                tid,
                "rest-import:" + batch["batch_id"],
                artifact_execution.execution_owner,
            )
        async with tx.aggregate(tid):
            result, error_data = await self._apply(
                tx,
                tenant,
                batch,
                batch_key,
                records,
                work,
                failed,
                skipped,
                facts,
                password_hash,
                logs,
                on_conflict,
                sync_mode,
                filename,
                reason,
                file_storage,
                artifact_execution,
                source_proof,
            )
        # The aggregate may advance B. Bind evidence only after its final epoch.
        if error_data:
            stamp = await user_transfer_fields.stamp(db, tx, tenant_id=tid)
            lineage = dict(batch["field_lineage"] or {})
            lineage["import_errors"] = make_lineage(
                tenant_id=tid,
                owner_id=tenant.actor_user_id,
                resource_id=batch["batch_id"],
                kind="import_errors",
                stamp=stamp,
                data=error_data,
                mode=sync_mode.value,
            )
            await tx.update_factual(
                batch_key,
                {"field_lineage": lineage},
                expected={"status": result.status},
            )
        return result

    async def execute(self, db, **request):
        command = await self.prepare_input(db, **request)
        tenant = command.values[0]
        with bounded_authorization_reads(db):
            async with authorization_transactions.scope(
                db,
                tenants=(TenantLock(tenant.tenant_id, LockMode.EXCLUSIVE),),
                budget=TransactionBudget(timeout_ms=60000),
            ) as tx:
                work = await self.plan(db, tx, command)
                await tx.lock(work.locks)
                return await self.apply(db, tx, work)

    @staticmethod
    def _work(records, resolutions, classified, on_conflict, sync_mode):
        new, exists, conflicts, out_of_scope = classified
        by_row = {item.row_num: item for item in resolutions}
        work = [("create", row, by_row[row.row_num]) for row in new]
        failed, skipped = list(conflicts) + list(out_of_scope), 0
        for row in exists:
            resolution = by_row[row.row_num]
            action = classify_sync_action(resolution.matched_by_employee_no, sync_mode)
            if action == SyncAction.REJECT_EMPLOYEE_NO_EXISTS:
                failed.append(
                    FailedRow(
                        row_num=row.row_num,
                        field="employee_no",
                        value=row.employee_no or "",
                        reason=f"employee_no 已存在，sync_mode={sync_mode.value} 拒绝",
                        error_code="AI_IMPORT_EMPLOYEE_NO_EXISTS",
                    )
                )
            elif (
                action in (SyncAction.UPDATE_SAFE, SyncAction.UPDATE_FULL)
                or on_conflict == "overwrite"
            ):
                work.append(("overwrite", row, resolution))
            elif on_conflict == "skip":
                skipped += 1
            else:
                failed.append(
                    FailedRow(
                        row_num=row.row_num,
                        field="user_name",
                        value=row.user_name,
                        reason="用户名已存在，严格冲突策略拒绝",
                        error_code="AI_IMPORT_USERNAME_DUPLICATE",
                    )
                )
        if on_conflict == "fail_fast" and failed:
            bad = {row.row_num for row in failed}
            failed.extend(_aborted(row) for row in records if row.row_num not in bad)
            return [], failed, 0
        if out_of_scope:
            return [], failed, skipped
        return work, failed, skipped

    async def _row(
        self, tx, tid, kind, row, resolution, facts, password_hash, sync_mode
    ):
        uid = resolution.target_user_id or resolution.prospective_user_id
        key = _key(ResourceKind.USER, tid, uid)
        if kind == "create":
            await tx.insert_reserved(
                key,
                {
                    "user_name": row.user_name,
                    "employee_no": row.employee_no or None,
                    "nickname": row.nickname or None,
                    "user_email": row.user_email or None,
                    "user_phone": row.user_phone or None,
                    "user_gender": row.user_gender,
                    "status": row.status,
                    "hashed_password": password_hash,
                },
            )
        else:
            old = next(
                item for item in facts[ResourceKind.USER] if item["user_id"] == uid
            )
            values = {
                name: getattr(row, name)
                for name in ("nickname", "user_email", "user_phone")
                if name in OVERWRITE_ALLOWED and getattr(row, name) is not None
            }
            values.update(
                {
                    name: getattr(row, name)
                    for name in ("user_gender", "status")
                    if name in OVERWRITE_ALLOWED
                }
            )
            if row.employee_no and "employee_no" in OVERWRITE_ALLOWED:
                values["employee_no"] = row.employee_no
            renamed = (
                resolution.matched_by_employee_no
                and sync_mode == EmployeeNoSyncMode.FULL_SYNC
                and old["user_name"] != row.user_name
            )
            if renamed:
                if ADMIN_USERNAME in (old["user_name"], row.user_name):
                    raise BusinessRuleException(
                        "不能通过导入修改系统管理员用户名",
                        error_code="AI_IMPORT_ADMIN_RENAME_FORBIDDEN",
                    )
                values["user_name"] = row.user_name
            if renamed or values.get("status", old["status"]) != old["status"]:
                values["auth_version"] = old["auth_version"] + 1
            await tx.update_held(key, values)
        if kind == "create" or (row.role_input and resolution.role_ids):
            await system._replace(
                tx,
                tid,
                ResourceKind.USER_ROLES,
                uid,
                {rid: {} for rid in resolution.role_ids},
                facts,
            )
        await system._replace(
            tx,
            tid,
            ResourceKind.USER_DEPTS,
            uid,
            {resolution.dept_id: {"is_primary": "Y"}},
            facts,
        )

    async def _process(
        self, tx, tid, work, facts, password_hash, on_conflict, sync_mode
    ):
        successes = overwritten = 0
        failures, progress = [], []
        for start in range(0, len(work), USER_IMPORT_CHUNK_SIZE):
            chunk_success = chunk_overwritten = 0
            chunk_failed = []
            async with tx.savepoint():
                for kind, row, resolution in work[
                    start : start + USER_IMPORT_CHUNK_SIZE
                ]:
                    try:
                        async with tx.savepoint():
                            await self._row(
                                tx,
                                tid,
                                kind,
                                row,
                                resolution,
                                facts,
                                password_hash,
                                sync_mode,
                            )
                    except (BusinessRuleException, IntegrityError) as exc:
                        if not _recoverable(exc):
                            raise
                        tx.assert_held()
                        failure = _make_failed_row_from_exc(
                            row, exc, _extract_error_code(exc)
                        )
                        if on_conflict == "fail_fast":
                            raise _StrictAbort(failure) from exc
                        chunk_failed.append(failure)
                    else:
                        # Row and then chunk accounting follows successful RELEASE.
                        chunk_success += kind == "create"
                        chunk_overwritten += kind == "overwrite"
            successes += chunk_success
            overwritten += chunk_overwritten
            failures.extend(chunk_failed)
            progress.append(
                {
                    "chunk_index": start // USER_IMPORT_CHUNK_SIZE,
                    "success_in_chunk": chunk_success + chunk_overwritten,
                    "failed_in_chunk": len(chunk_failed),
                }
            )
        return successes, overwritten, failures, progress

    async def _apply(
        self,
        tx,
        tenant,
        batch,
        batch_key,
        records,
        work,
        failed,
        skipped,
        facts,
        password_hash,
        logs,
        on_conflict,
        sync_mode,
        filename,
        reason,
        storage,
        execution,
        source_proof=None,
    ):
        started = datetime.now(UTC)
        await tx.update_held(
            batch_key,
            {"status": "RUNNING", "started_at": started},
            expected={"status": "PREVIEW_DONE"},
        )

        async def log(key, event, detail, from_status=None, to_status=None):
            await tx.insert_reserved(
                key,
                {
                    "batch_id": batch["batch_id"],
                    "operator_id": tenant.actor_user_id,
                    "event": event,
                    "detail": detail,
                    "from_status": from_status,
                    "to_status": to_status,
                },
            )

        await log(
            logs[0],
            "EXECUTE_START",
            {
                "operator_id": tenant.actor_user_id,
                "started_at": started.isoformat(),
                "filename": filename,
                "reason": reason,
                "on_conflict": on_conflict,
                "sync_mode": sync_mode.value,
            },
            "PREVIEW_DONE",
            "RUNNING",
        )
        try:
            async with tx.savepoint():
                successes, overwritten, row_failed, progress = await self._process(
                    tx,
                    tenant.tenant_id,
                    work,
                    facts,
                    password_hash,
                    on_conflict,
                    sync_mode,
                )
        except _StrictAbort as exc:
            successes = overwritten = skipped = 0
            failed = [
                exc.failed if row.row_num == exc.failed.row_num else _aborted(row)
                for row in records
            ]
            progress = []
        else:
            failed.extend(row_failed)
        end = _determine_end_status(successes + overwritten, len(failed))
        # Candidate completion time: logs stay invisible until FILE and COMMIT
        # succeed, and a failed FILE rolls this timestamp back with the batch.
        finished = datetime.now(UTC)
        for index, detail in enumerate(progress, 1):
            await log(
                logs[index], "CHUNK_PROGRESS", detail | {"total_chunks": len(progress)}
            )
        await log(
            logs[-1],
            "EXECUTE_FINISH",
            {
                "success_count": successes,
                "overwritten_count": overwritten,
                "skipped_count": skipped,
                "failed_count": len(failed),
                "finished_at": finished.isoformat(),
                "duration_ms": int((finished - started).total_seconds() * 1000),
            },
            "RUNNING",
            end.value,
        )
        file_key = None
        failed = safe_failed_rows(failed)
        data = b""
        if failed or source_proof is not None:
            tx.assert_held(batch_key)
            data = _failed_rows_to_xlsx_bytes(failed) if failed else b""
            if len(data) > _MAX_BYTES:
                raise _limit()
            tx.assert_held(batch_key)
            identity = execution.identity("import_errors", batch["batch_id"])
            tx.seal_insertions()
            with tx.file_io(max_reads=4, max_total_bytes=40 * 1024 * 1024):
                if source_proof is not None:
                    source_key, source_identity, source_hash = source_proof
                    source = await storage.read_artifact(
                        source_key, identity=source_identity
                    )
                    if (
                        _compute_file_sha256(source) != source_hash
                        or source_key != batch["file_storage_key"]
                    ):
                        raise _stale()
                if failed:
                    file_key = await storage.save_artifact(data, identity=identity)
                    if type(file_key) is not str or file_key != identity.storage_key:
                        raise BusinessRuleException(
                            "文件归属或完整性待核对",
                            error_code="AI_ARTIFACT_UNVERIFIED",
                        )
                    if (
                        source_proof is not None
                        and await storage.read_artifact(file_key, identity=identity)
                        != data
                    ):
                        raise _stale()
            tx.assert_held(batch_key)
        await tx.update_factual(
            batch_key,
            {
                "status": end.value,
                "success_count": successes,
                "overwritten_count": overwritten,
                "skipped_count": skipped,
                "failed_count": len(failed),
                "failed_rows_file": file_key,
                "finished_at": finished,
            },
            expected={"status": "RUNNING"},
        )
        return ImportResult(
            batch_id=batch["batch_id"],
            status=end.value,
            success_count=successes,
            overwritten_count=overwritten,
            skipped_count=skipped,
            failed_count=len(failed),
            failed_rows_file=file_key,
            failed_rows_preview=failed[:FAILED_ROWS_PREVIEW_LIMIT],
        ), data


import_authorization_candidate = ImportAuthorizationCandidate()
