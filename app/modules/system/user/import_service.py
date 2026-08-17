"""用户导入预览、执行、查询、取消与清理主流程。

职责：
- dry_run_import_users：四象限分类、preview_token 和 Redis cache
- batch_create_users_from_records：
  - 校验 file_sha256、records_hash 和 operator_id，确保执行内容与预览一致
  - CAS PREVIEW_DONE → RUNNING，保证幂等和并发安全
  - 每 100 行一个 chunk，每行使用 savepoint 隔离可恢复错误
  - 支持 skip / overwrite / fail_fast，并持久化批次日志和失败清单
"""

import hashlib
import io
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from openpyxl import Workbook
from sqlalchemy import func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import STATUS_ENABLED, USER_ROLE_CODE
from app.core import redis as redis_module
from app.core.exceptions import (
    AuthorizationException,
    BusinessException,
    BusinessRuleException,
    NotFoundException,
    UnprocessableEntityException,
)
from app.core.file_storage import FileStorage, get_file_storage
from app.core.id_generator import next_id
from app.core.security import get_password_hash
from app.db.base import user_depts, user_roles
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.service.grant_authority import grant_authority_service
from app.modules.system.service.user_role_assignment_service import (
    user_role_assignment_service,
)
from app.modules.system.user.constants import (
    FAILED_ROWS_PREVIEW_LIMIT,
    MAX_PREVIEW_RECORDS,
    OVERWRITE_ALLOWED,
    RECOVERABLE_ERROR_CODES,
    TERMINAL_STATUSES,
    USER_IMPORT_CHUNK_SIZE,
    EmployeeNoSyncMode,
    ImportBatchStatus,
)
from app.modules.system.user.helpers import get_default_password
from app.modules.system.user.import_state import (
    _transition_batch_status,
    validate_reason_consistency,
    validate_transition,
)
from app.modules.system.user.import_validator import (
    SyncAction,
    classify_sync_action,
    resolve_dept,
    resolve_existing_user,
    resolve_role_input,
)
from app.modules.system.user.models import UserImportBatch, UserImportBatchLog
from app.modules.system.user.schemas import (
    FailedRow,
    ImportDryRunResult,
    ImportResult,
    UserImportBatchQuery,
    UserImportRecord,
)

#: Redis 只缓存 preview_token 到 batch_id 的映射，事实源仍是数据库。
_PREVIEW_REDIS_PREFIX = "user_import:preview:"
_PREVIEW_REDIS_TTL_SECONDS = 600

#: batch_id / preview_token 生成长度
_BATCH_ID_LENGTH = 32
_PREVIEW_TOKEN_LENGTH = 32


@dataclass(frozen=True)
class ImportAuthorizationResolution:
    """Frozen name-resolution facts used by import authorization and writes."""

    row_num: int
    dept_id: int | None
    dept_error: tuple[str, str] | None
    role_ids: tuple[int, ...] | None
    role_error: tuple[str, str] | None
    target_user_id: int | None
    matched_by_employee_no: bool
    target_role_ids: tuple[int, ...]
    target_dept_ids: tuple[int, ...]
    target_status: str
    prospective_user_id: int | None


def _generate_batch_id() -> str:
    """生成 URL-safe batch_id。"""
    return secrets.token_urlsafe(_BATCH_ID_LENGTH)[:_BATCH_ID_LENGTH]


def _generate_preview_token() -> str:
    """生成不可枚举的 URL-safe preview_token。"""
    return secrets.token_urlsafe(_PREVIEW_TOKEN_LENGTH)[:_PREVIEW_TOKEN_LENGTH]


def _compute_file_sha256(file_bytes: bytes) -> str:
    """计算文件哈希，执行时用于校验预览输入未变化。"""
    return hashlib.sha256(file_bytes).hexdigest()


def _compute_records_hash(records: list[UserImportRecord]) -> str:
    """将 records 稳定序列化后计算哈希。

    排序保证：相同 records → 相同 hash（防 list 顺序变化误报）。
    row_num 是 record 的天然排序键。
    """
    sorted_records = sorted(records, key=lambda r: r.row_num)
    payload = json.dumps(
        [r.model_dump(mode="json") for r in sorted_records],
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _truncate(
    items: list,
    limit: int = MAX_PREVIEW_RECORDS,
) -> tuple[list, bool]:
    """截断到 limit，返回 (truncated_list, truncated_flag)。"""
    if len(items) > limit:
        return items[:limit], True
    return items, False


def _validate_reason(reason: str) -> str:
    """对业务理由做 service 层防御性校验。

    API 层 ReasonSchema 已校验，service 层 defense-in-depth（AI tool 直接调用时
    也能拦住）。strip 后 1-256 字符；不通过抛 AI_IMPORT_REASON_REQUIRED。
    """
    if reason is None:
        raise BusinessRuleException(
            "reason 必填",
            error_code="AI_IMPORT_REASON_REQUIRED",
        )
    stripped = reason.strip()
    if not stripped or len(stripped) > 256:
        raise BusinessRuleException(
            "reason 必填且长度 1-256 字符",
            error_code="AI_IMPORT_REASON_REQUIRED",
        )
    return stripped


async def _resolve_import_authorization_targets(
    db: AsyncSession,
    records: list[UserImportRecord],
    *,
    has_role_column: bool,
    prospective_user_ids: dict[int, int] | None = None,
) -> list[ImportAuthorizationResolution]:
    """Resolve every authorization reference into one comparable batch snapshot."""
    stable_user_ids = prospective_user_ids if prospective_user_ids is not None else {}
    default_role_id = await db.scalar(
        select(Role.role_id).where(
            Role.role_code == USER_ROLE_CODE,
            Role.status == STATUS_ENABLED,
        )
    )
    result: list[ImportAuthorizationResolution] = []
    for record in records:
        dept_id: int | None = None
        dept_error: tuple[str, str] | None = None
        try:
            dept_id = int(await resolve_dept(db, record.dept_input))
        except BusinessRuleException as exc:
            dept_error = (exc.error_code, str(exc))

        existing, matched_by_employee_no = await resolve_existing_user(db, record)
        role_ids: tuple[int, ...] | None = None
        role_error: tuple[str, str] | None = None
        if record.role_input:
            try:
                role_ids = tuple(
                    sorted(
                        int(role_id)
                        for role_id in await resolve_role_input(
                            db,
                            record.role_input,
                        )
                    )
                )
            except BusinessRuleException as exc:
                role_error = (exc.error_code, str(exc))
        elif existing is None:
            if default_role_id is None:
                role_error = (
                    "USER_DEFAULT_ROLE_NOT_AVAILABLE",
                    "默认角色不存在或已禁用",
                )
            else:
                role_ids = (int(default_role_id),)
        elif has_role_column:
            role_ids = tuple(sorted(int(role.role_id) for role in existing.roles))

        if existing is None:
            prospective_user_id = stable_user_ids.setdefault(
                record.row_num,
                next_id(),
            )
            target_user_id = None
            target_role_ids: tuple[int, ...] = ()
            target_dept_ids: tuple[int, ...] = ()
            target_status = record.status
        else:
            prospective_user_id = None
            target_user_id = int(existing.user_id)
            target_role_ids = tuple(
                sorted(int(role.role_id) for role in existing.roles)
            )
            target_dept_ids = tuple(
                sorted(int(dept.dept_id) for dept in existing.depts)
            )
            target_status = str(existing.status)

        result.append(
            ImportAuthorizationResolution(
                row_num=record.row_num,
                dept_id=dept_id,
                dept_error=dept_error,
                role_ids=role_ids,
                role_error=role_error,
                target_user_id=target_user_id,
                matched_by_employee_no=matched_by_employee_no,
                target_role_ids=target_role_ids,
                target_dept_ids=target_dept_ids,
                target_status=target_status,
                prospective_user_id=prospective_user_id,
            )
        )
    return result


def _failed_resolution(
    record: UserImportRecord,
    *,
    field: str,
    error: tuple[str, str],
) -> FailedRow:
    return FailedRow(
        row_num=record.row_num,
        field=field,
        value=(record.dept_input if field == "dept_input" else record.role_input or ""),
        reason=error[1],
        error_code=error[0],
    )


async def _classify_records(
    db: AsyncSession,
    records: list[UserImportRecord],
    current_user: User,
    *,
    has_role_column: bool,
    resolutions: list[ImportAuthorizationResolution] | None = None,
) -> tuple[
    list[UserImportRecord],
    list[UserImportRecord],
    list[FailedRow],
    list[FailedRow],
]:
    """Classify rows using the same materialized role policy as online writers."""
    if resolutions is None:
        resolutions = await _resolve_import_authorization_targets(
            db,
            records,
            has_role_column=has_role_column,
        )
    by_row = {resolution.row_num: resolution for resolution in resolutions}
    authority = await grant_authority_service.build(db, int(current_user.user_id))
    ignored_user_ids = frozenset(
        resolution.prospective_user_id
        for resolution in resolutions
        if resolution.prospective_user_id is not None
    )
    new_records: list[UserImportRecord] = []
    exists_records: list[UserImportRecord] = []
    conflict_records: list[FailedRow] = []
    out_of_scope_records: list[FailedRow] = []

    for record in records:
        resolution = by_row[record.row_num]
        if resolution.dept_error is not None:
            conflict_records.append(
                _failed_resolution(
                    record,
                    field="dept_input",
                    error=resolution.dept_error,
                )
            )
            continue
        if resolution.role_error is not None:
            conflict_records.append(
                _failed_resolution(
                    record,
                    field="role_input",
                    error=resolution.role_error,
                )
            )
            continue
        assert resolution.dept_id is not None
        if (
            authority.accessible_dept_ids is not None
            and resolution.dept_id not in authority.accessible_dept_ids
        ):
            out_of_scope_records.append(
                FailedRow(
                    row_num=record.row_num,
                    field="dept_input",
                    value=record.dept_input,
                    reason="部门不在当前用户的数据权限范围内",
                    error_code="AI_IMPORT_DEPT_OUT_OF_SCOPE",
                )
            )
            continue

        if resolution.role_ids is not None:
            try:
                await user_role_assignment_service.validate_import_role_assignment(
                    db,
                    actor_user_id=int(current_user.user_id),
                    target_user_id=resolution.target_user_id,
                    target_user_name=record.user_name,
                    target_status=resolution.target_status,
                    role_ids=list(resolution.role_ids),
                    dept_ids=[resolution.dept_id],
                    authority=authority,
                    ignored_user_ids=ignored_user_ids,
                    prospective_user_id=resolution.prospective_user_id,
                )
            except AuthorizationException as exc:
                out_of_scope_records.append(
                    FailedRow(
                        row_num=record.row_num,
                        field="role_input",
                        value=record.role_input or "",
                        reason=str(exc),
                        error_code="AI_IMPORT_ROLE_OUT_OF_SCOPE",
                    )
                )
                continue
            except BusinessRuleException as exc:
                if exc.error_code in {
                    "USER_ROLE_SELF_ASSIGNMENT_FORBIDDEN",
                    "AUTHORIZATION_SNAPSHOT_STALE",
                }:
                    out_of_scope_records.append(
                        FailedRow(
                            row_num=record.row_num,
                            field="role_input",
                            value=record.role_input or "",
                            reason=str(exc),
                            error_code="AI_IMPORT_ROLE_OUT_OF_SCOPE",
                        )
                    )
                else:
                    conflict_records.append(
                        FailedRow(
                            row_num=record.row_num,
                            field="role_input",
                            value=record.role_input or "",
                            reason=str(exc),
                            error_code=exc.error_code,
                        )
                    )
                continue

        if resolution.target_user_id is None:
            new_records.append(record)
        else:
            exists_records.append(record)

    return new_records, exists_records, conflict_records, out_of_scope_records


async def _lock_import_authorization_targets(
    db: AsyncSession,
    records: list[UserImportRecord],
    current_user: User,
    *,
    has_role_column: bool,
) -> tuple[User, list[ImportAuthorizationResolution]]:
    """Lock a complete import snapshot and reject reference-set drift."""
    prospective_user_ids: dict[int, int] = {}
    before = await _resolve_import_authorization_targets(
        db,
        records,
        has_role_column=has_role_column,
        prospective_user_ids=prospective_user_ids,
    )
    role_ids = {
        role_id
        for resolution in before
        for role_id in (*resolution.target_role_ids, *(resolution.role_ids or ()))
    }
    dept_ids = {
        dept_id
        for resolution in before
        for dept_id in (
            *resolution.target_dept_ids,
            *((resolution.dept_id,) if resolution.dept_id is not None else ()),
        )
    }
    target_user_ids = {
        resolution.target_user_id
        for resolution in before
        if resolution.target_user_id is not None
    }
    try:
        actor = await user_role_assignment_service.lock_import_targets(
            db,
            actor_user_id=int(current_user.user_id),
            target_user_ids=target_user_ids,
            role_ids=role_ids,
            dept_ids=dept_ids,
        )
    except (BusinessRuleException, NotFoundException) as exc:
        raise BusinessRuleException(
            "授权事实已变化，请重试",
            error_code="AUTHORIZATION_SNAPSHOT_STALE",
        ) from exc

    after = await _resolve_import_authorization_targets(
        db,
        records,
        has_role_column=has_role_column,
        prospective_user_ids=prospective_user_ids,
    )
    if after != before:
        raise BusinessRuleException(
            "授权事实已变化，请重试",
            error_code="AUTHORIZATION_SNAPSHOT_STALE",
        )
    await user_role_assignment_service.ensure_import_permissions(
        db,
        actor_user_id=int(current_user.user_id),
        has_role_column=has_role_column,
    )
    return actor, after


async def dry_run_import_users(
    db: AsyncSession,
    records: list[UserImportRecord],
    current_user,
    file_bytes: bytes,
    filename: str,
    reason: str,
    *,
    on_conflict: Literal["skip", "overwrite", "fail_fast"] = "skip",
    has_role_column: bool | None = None,
) -> tuple[ImportDryRunResult, UserImportBatch]:
    """预检导入内容并生成 preview_token。

    流程：
    1. 校验业务理由
    2. 算 file_sha256 / records_hash / preview_token / batch_id
    3. INSERT sys_user_import_batch (status=CREATED)
    4. 四象限分类（new / exists / conflict / out_of_scope）
    5. 截断每个象限到 MAX_PREVIEW_RECORDS
    6. CAS 状态 CREATED → PREVIEW_DONE（validate_transition 校验合法）
    7. 在 Redis 缓存 preview_token → batch_id，TTL 10 分钟
    8. 返回 (ImportDryRunResult, batch)，调用方拿 preview_token 给前端

    Notes:
        - Service 层不 commit（API 层负责）；outer-transaction rollback 时 INSERT 自动撤销
        - Redis 缓存丢失时，执行仍可凭 preview_token 回查数据库

    Raises:
        BusinessRuleException: ``AI_IMPORT_REASON_REQUIRED`` — reason 缺失或超长
    """
    reason_clean = _validate_reason(reason)
    effective_has_role_column = (
        has_role_column
        if has_role_column is not None
        else any(record.role_input is not None for record in records)
    )

    file_sha256 = _compute_file_sha256(file_bytes)
    records_hash = _compute_records_hash(records)
    batch_id = _generate_batch_id()
    preview_token = _generate_preview_token()

    # 1. INSERT batch（CREATED）
    batch = UserImportBatch(
        batch_id=batch_id,
        operator_id=current_user.user_id,
        filename=filename,
        file_sha256=file_sha256,
        records_hash=records_hash,
        total_rows=len(records),
        preview_token=preview_token,
        on_conflict=on_conflict,
        reason=reason_clean,
        status=ImportBatchStatus.CREATED,
    )
    db.add(batch)
    await db.flush()  # 拿默认 server_default 字段（created_at 等）

    # 空文件或分类异常必须把已创建批次收口为 FAILED。
    # 反例（旧行为）：batch 停留在 CREATED 成僵尸行，审计看到「创建一年后还是 CREATED」
    if len(records) == 0:
        await _transition_batch_status(
            db,
            batch_id,
            ImportBatchStatus.CREATED,
            ImportBatchStatus.FAILED,
        )
        raise BusinessRuleException(
            "文件解析后 0 行有效记录",
            error_code="AI_IMPORT_EMPTY_FILE",
        )

    try:
        # 2. 四象限分类
        (
            new_records,
            exists_records,
            conflict_records,
            out_of_scope_records,
        ) = await _classify_records(
            db,
            records,
            current_user,
            has_role_column=effective_has_role_column,
        )

        # 3. 截断预览记录。
        new_truncated_list, new_trunc = _truncate(new_records)
        exists_truncated_list, exists_trunc = _truncate(exists_records)
        conflict_truncated_list, conflict_trunc = _truncate(conflict_records)
        oos_truncated_list, oos_trunc = _truncate(out_of_scope_records)

        # 4. 更新批次汇总并进入 PREVIEW_DONE。
        validate_transition(ImportBatchStatus.CREATED, ImportBatchStatus.PREVIEW_DONE)
        batch.summary_new = len(new_records)
        batch.summary_exists = len(exists_records)
        batch.summary_conflict = len(conflict_records)
        batch.summary_out_of_scope = len(out_of_scope_records)
        batch.status = ImportBatchStatus.PREVIEW_DONE
        await db.flush()

        # 5. Redis 只缓存 batch_id。
        cache_payload = json.dumps({"batch_id": batch_id})
        await redis_module.redis_client.setex(
            f"{_PREVIEW_REDIS_PREFIX}{preview_token}",
            _PREVIEW_REDIS_TTL_SECONDS,
            cache_payload,
        )
    except Exception:
        # 预览阶段任何异常都必须把批次收口为 FAILED。
        # 防 batch 停留 CREATED 成僵尸行（审计反查看到「创建一年后还是 CREATED」）
        await _transition_batch_status(
            db,
            batch_id,
            ImportBatchStatus.CREATED,
            ImportBatchStatus.FAILED,
        )
        raise

    # 6. 构造 result（conflict/out_of_scope 截断展示）
    result = ImportDryRunResult(
        total=len(records),
        new_records=new_truncated_list,
        exists_records=exists_truncated_list,
        conflict_records=conflict_truncated_list,
        out_of_scope_records=oos_truncated_list,
        new_records_truncated=new_trunc,
        exists_records_truncated=exists_trunc,
        conflict_records_truncated=conflict_trunc,
        out_of_scope_records_truncated=oos_trunc,
    )
    return result, batch


async def get_batch_by_preview_token(
    db: AsyncSession,
    preview_token: str,
) -> UserImportBatch | None:
    """凭 preview_token 反查批次，先查 Redis，未命中时回退数据库。"""
    # 1. Redis 加速
    cached = await redis_module.redis_client.get(
        f"{_PREVIEW_REDIS_PREFIX}{preview_token}"
    )
    if cached:
        batch_id = json.loads(cached).get("batch_id")
        if batch_id:
            row = (
                await db.execute(
                    select(UserImportBatch).where(UserImportBatch.batch_id == batch_id)
                )
            ).scalar_one_or_none()
            if row is not None:
                return row
            # Redis 映射可能是脏数据，批次不存在时回退数据库反查。

    # 2. DB 反查（cache miss / 脏数据 fallback）
    return (
        await db.execute(
            select(UserImportBatch).where(
                UserImportBatch.preview_token == preview_token
            )
        )
    ).scalar_one_or_none()


# ============================================================================
# ============ 执行导入 ============
# ============================================================================


def _extract_constraint_name(exc: IntegrityError) -> str:
    """提取 PostgreSQL UNIQUE 约束名（asyncpg orig.diag.constraint_name）。"""
    orig = getattr(exc, "orig", None)
    if orig is not None:
        diag = getattr(orig, "diag", None)
        if diag is not None:
            name = getattr(diag, "constraint_name", None)
            if name:
                return name
    return str(exc)


def _classify_integrity_error(exc: IntegrityError) -> str:
    """将 UNIQUE IntegrityError 转换为稳定业务错误码。

    - ``ix_sys_user_user_name`` → ``AI_IMPORT_USERNAME_DUPLICATE``
    - ``uq_sys_user_employee_no`` → ``AI_IMPORT_EMPLOYEE_NO_DUPLICATE``
    - 其他 → ``AI_IMPORT_UNKNOWN``（致命，应让上层抛出）

    实现：用约束名子串匹配（PostgreSQL 索引名稳定）。
    """
    name = _extract_constraint_name(exc)
    if "user_name" in name:
        return "AI_IMPORT_USERNAME_DUPLICATE"
    if "employee_no" in name:
        return "AI_IMPORT_EMPLOYEE_NO_DUPLICATE"
    return "AI_IMPORT_UNKNOWN_INTEGRITY_ERROR"


def _extract_error_code(exc: Exception) -> str:
    """从异常提取 error_code（BusinessException.error_code / IntegrityError 分类）。"""
    if isinstance(exc, IntegrityError):
        return _classify_integrity_error(exc)
    code = getattr(exc, "error_code", "")
    return code or exc.__class__.__name__


async def _write_batch_log(
    db: AsyncSession,
    batch: UserImportBatch,
    operator: User,
    *,
    event: str,
    from_status: ImportBatchStatus | None = None,
    to_status: ImportBatchStatus | None = None,
    detail: dict,
) -> None:
    """写入一条批次审计日志。

    _transition_batch_status 内部已经写过部分状态转换 log；本 helper 用于
    EXECUTE_START / CHUNK_PROGRESS / EXECUTE_FINISH 等业务事件。
    """
    db.add(
        UserImportBatchLog(
            log_id=str(next_id()),
            batch_id=batch.batch_id,
            operator_id=operator.user_id,
            event=event,
            from_status=from_status,
            to_status=to_status,
            detail=detail,
        )
    )
    await db.flush()


def _failed_rows_to_xlsx_bytes(failed_rows: list[FailedRow]) -> bytes:
    """生成失败行 Excel 文件。

    列顺序固定：row_num / field / value / reason / error_code。
    用 openpyxl 内存 workbook → BytesIO，避免磁盘 IO。
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "failed_rows"
    ws.append(["row_num", "field", "value", "reason", "error_code"])
    for fr in failed_rows:
        ws.append([fr.row_num, fr.field, fr.value, fr.reason, fr.error_code])

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


def _determine_end_status(
    success_count: int,
    failed_count: int,
) -> ImportBatchStatus:
    """根据成功和失败数量判断执行终态。

    - failed_count == 0 → SUCCESS（全部成功）
    - failed_count > 0 且 success_count > 0 → PARTIAL_SUCCESS
    - failed_count > 0 且 success_count == 0 → FAILED
    """
    if failed_count == 0:
        return ImportBatchStatus.SUCCESS
    if success_count > 0:
        return ImportBatchStatus.PARTIAL_SUCCESS
    return ImportBatchStatus.FAILED


async def _process_create_row(
    db: AsyncSession,
    record: UserImportRecord,
    hashed_password: str,
    current_user: User,
    resolution: ImportAuthorizationResolution,
    *,
    has_role_column: bool,
    ignored_user_ids: frozenset[int],
) -> None:
    """新建用户行级处理（INSERT user + bind roles + bind dept）。

    失败抛异常 → 上层 savepoint 自动 ROLLBACK。
    """
    assert resolution.dept_id is not None
    assert resolution.role_ids is not None
    assert resolution.prospective_user_id is not None

    new_user = User(
        user_id=resolution.prospective_user_id,
        user_name=record.user_name,
        employee_no=record.employee_no or None,
        nickname=record.nickname or None,
        user_email=record.user_email or None,
        user_phone=record.user_phone or None,
        user_gender=record.user_gender,
        status=record.status,
        hashed_password=hashed_password,
    )
    db.add(new_user)
    await db.flush()  # 触发 INSERT，捕获 UNIQUE IntegrityError

    await user_role_assignment_service.assign_imported_user_roles(
        db,
        actor_user_id=current_user.user_id,
        target_user_id=new_user.user_id,
        role_ids=(list(resolution.role_ids) if has_role_column else None),
        dept_ids=[resolution.dept_id],
        has_role_column=has_role_column,
        ignored_user_ids=ignored_user_ids,
    )
    await db.execute(
        insert(user_depts),
        [
            {
                "user_id": new_user.user_id,
                "dept_id": resolution.dept_id,
                "is_primary": "Y",
            }
        ],
    )
    await db.flush()


async def _process_overwrite_row(
    db: AsyncSession,
    record: UserImportRecord,
    existing: User,
    resolution: ImportAuthorizationResolution,
) -> None:
    """覆盖已有用户时只更新 OVERWRITE_ALLOWED 字段。

    user_name / hashed_password / user_id / create_time 永不覆盖。
    """
    assert resolution.dept_id is not None

    # 应用 OVERWRITE_ALLOWED 字段
    if "nickname" in OVERWRITE_ALLOWED and record.nickname is not None:
        existing.nickname = record.nickname
    if "user_email" in OVERWRITE_ALLOWED and record.user_email is not None:
        existing.user_email = record.user_email
    if "user_phone" in OVERWRITE_ALLOWED and record.user_phone is not None:
        existing.user_phone = record.user_phone
    if "user_gender" in OVERWRITE_ALLOWED:
        existing.user_gender = record.user_gender
    if "status" in OVERWRITE_ALLOWED:
        existing.status = record.status
    if "employee_no" in OVERWRITE_ALLOWED and record.employee_no:
        existing.employee_no = record.employee_no

    await db.flush()

    # roles 和 dept 使用全量重置语义。
    if record.role_input and resolution.role_ids:
        await db.execute(
            user_roles.delete().where(user_roles.c.user_id == existing.user_id)
        )
        await db.execute(
            insert(user_roles),
            [
                {"user_id": existing.user_id, "role_id": role_id}
                for role_id in resolution.role_ids
            ],
        )

    await db.execute(
        user_depts.delete().where(user_depts.c.user_id == existing.user_id)
    )
    await db.execute(
        insert(user_depts),
        [
            {
                "user_id": existing.user_id,
                "dept_id": resolution.dept_id,
                "is_primary": "Y",
            }
        ],
    )
    await db.flush()


async def _handle_idempotent_replay(
    db: AsyncSession,
    batch_id: str,
) -> ImportResult:
    """CAS 失败后读取最新批次，按当前状态返回幂等响应或抛错。

    使用 ``populate_existing`` 强制从 DB 重读，绕开 identity map 缓存的 stale 实例
    （raw UPDATE 不更新 ORM session 的实例属性）。
    """
    fresh = (
        await db.execute(
            select(UserImportBatch)
            .where(UserImportBatch.batch_id == batch_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if fresh is None:
        raise UnprocessableEntityException(
            "preview_token 无效或已过期",
            error_code="AI_IMPORT_PREVIEW_INVALID",
        )

    if fresh.status in (ImportBatchStatus.SUCCESS, ImportBatchStatus.PARTIAL_SUCCESS):
        return ImportResult(
            batch_id=fresh.batch_id,
            status=fresh.status.value,
            success_count=fresh.success_count,
            skipped_count=fresh.skipped_count,
            overwritten_count=fresh.overwritten_count,
            failed_count=fresh.failed_count,
            failed_rows_file=fresh.failed_rows_file,
            idempotent_replay=True,
        )
    if fresh.status == ImportBatchStatus.RUNNING:
        raise UnprocessableEntityException(
            "批次正在执行中，请等待",
            error_code="AI_IMPORT_BATCH_RUNNING",
        )
    # FAILED / EXPIRED / CANCELLED → 不能重放
    raise UnprocessableEntityException(
        f"批次已 {fresh.status.value}，不能重复执行",
        error_code="AI_IMPORT_ALREADY_EXECUTED",
    )


async def batch_create_users_from_records(
    db: AsyncSession,
    records: list[UserImportRecord],
    *,
    preview_token: str,
    file_bytes: bytes,
    filename: str,
    reason: str,
    current_user: User,
    on_conflict: Literal["skip", "overwrite", "fail_fast"] = "skip",
    sync_mode: EmployeeNoSyncMode = EmployeeNoSyncMode.CREATE_ONLY,
    file_storage: FileStorage | None = None,
    has_role_column: bool | None = None,
) -> ImportResult:
    """执行批量新建或更新。

    流程（#2.19-2.22 + #2.25-2.28 全套）：
    1. 凭 preview_token 反查批次，数据库是事实源
    2. 校验预览和执行的业务理由一致
    3. 校验 file_sha256、records_hash 和 operator_id 一致
    4. CAS PREVIEW_DONE → RUNNING
       - CAS 失败 → 幂等重放或抛错（详见 _handle_idempotent_replay）
    5. dry_run 二次跑分类（防 dry_run 后数据变化）→ conflict + oos 直接进 failed_rows
    6. exists_records 按 sync_mode + on_conflict 分流：skipped / failed / overwrite
    7. 每 100 行一个 chunk，使用行级 savepoint 和可恢复错误白名单
    8. 写 failed_rows xlsx 文件 → failed_rows_file
    9. 状态转 SUCCESS / PARTIAL_SUCCESS / FAILED + counts + finished_at
    10. 返回 ImportResult（含 failed_rows_preview 前 20 条）

    Notes:
        - Service 层不 commit（API 层负责）；outer-transaction rollback 时全部撤销
        - chunk / row 都用 ``db.begin_nested()`` 嵌套 SAVEPOINT，兼容 outer-transaction 测试 fixture

    Raises:
        BusinessRuleException:
            - ``AI_IMPORT_PREVIEW_INVALID`` — token / file / records / operator 不匹配
            - ``AI_IMPORT_REASON_MISMATCH`` — execute reason 与 preview 不一致
            - ``AI_IMPORT_BATCH_RUNNING`` — 并发 execute 同 token，后者抛此
            - ``AI_IMPORT_ALREADY_EXECUTED`` — FAILED/EXPIRED/CANCELLED 重放
            - ``AI_IMPORT_DEFAULT_PASSWORD_NOT_SET`` — sys_config 未配置默认密码
            - ``AI_IMPORT_DEFAULT_PASSWORD_INVALID`` — prod 仍使用公开初始化密码
    """
    storage = file_storage or get_file_storage()
    reason_clean = _validate_reason(reason)
    effective_has_role_column = (
        has_role_column
        if has_role_column is not None
        else any(record.role_input is not None for record in records)
    )

    # 1. 反查 batch
    batch = await get_batch_by_preview_token(db, preview_token)
    if batch is None:
        raise UnprocessableEntityException(
            "preview_token 无效或已过期",
            error_code="AI_IMPORT_PREVIEW_INVALID",
        )

    # 2. 业务理由一致性。
    validate_reason_consistency(batch.reason, reason_clean)

    # 3. 文件、记录和操作人一致性。
    expected_file_sha256 = _compute_file_sha256(file_bytes)
    expected_records_hash = _compute_records_hash(records)
    if (
        batch.file_sha256 != expected_file_sha256
        or batch.records_hash != expected_records_hash
        or batch.operator_id != current_user.user_id
    ):
        raise BusinessRuleException(
            "preview_token 三重校验失败（file_sha256 / records_hash / operator_id 不匹配）",
            error_code="AI_IMPORT_PREVIEW_INVALID",
        )

    # 4. CAS 进入 RUNNING，保证并发与幂等。
    started_at = datetime.now()
    cas_ok = await _transition_batch_status(
        db,
        batch.batch_id,
        ImportBatchStatus.PREVIEW_DONE,
        ImportBatchStatus.RUNNING,
        started_at=started_at,
    )
    if not cas_ok:
        return await _handle_idempotent_replay(db, batch.batch_id)
    # raw UPDATE 不更新 ORM session 实例；refresh 让后续读取 / 测试断言看到新状态
    await db.refresh(batch)

    # 5. 写 EXECUTE_START 日志。
    await _write_batch_log(
        db,
        batch,
        current_user,
        event="EXECUTE_START",
        from_status=ImportBatchStatus.PREVIEW_DONE,
        to_status=ImportBatchStatus.RUNNING,
        detail={
            "operator_id": current_user.user_id,
            "started_at": started_at.isoformat(),
            "reason": reason_clean,
            "filename": filename,
            "on_conflict": on_conflict,
            "sync_mode": sync_mode.value,
        },
    )

    (
        authorization_actor,
        authorization_resolutions,
    ) = await _lock_import_authorization_targets(
        db,
        records,
        current_user,
        has_role_column=effective_has_role_column,
    )
    resolutions_by_row = {
        resolution.row_num: resolution for resolution in authorization_resolutions
    }
    ignored_user_ids = frozenset(
        resolution.prospective_user_id
        for resolution in authorization_resolutions
        if resolution.prospective_user_id is not None
    )

    # 6. 二次跑分类（防 dry_run 后数据变化）
    (
        new_records,
        exists_records,
        conflict_records,
        out_of_scope_records,
    ) = await _classify_records(
        db,
        records,
        authorization_actor,
        has_role_column=effective_has_role_column,
        resolutions=authorization_resolutions,
    )
    failed_rows: list[FailedRow] = list(conflict_records) + list(out_of_scope_records)
    skipped_count = 0
    overwritten_count = 0
    success_count = 0

    # 7. exists_records 按 sync_mode + on_conflict 分流
    rows_to_create: list[UserImportRecord] = list(new_records)
    rows_to_overwrite: list[tuple[UserImportRecord, User]] = []

    for record in exists_records:
        resolution = resolutions_by_row[record.row_num]
        assert resolution.target_user_id is not None
        existing = await db.scalar(
            select(User).where(User.user_id == resolution.target_user_id)
        )
        if existing is None:
            raise BusinessRuleException(
                "授权事实已变化，请重试",
                error_code="AUTHORIZATION_SNAPSHOT_STALE",
            )
        action = classify_sync_action(resolution.matched_by_employee_no, sync_mode)

        if action == SyncAction.REJECT_EMPLOYEE_NO_EXISTS:
            failed_rows.append(
                FailedRow(
                    row_num=record.row_num,
                    field="employee_no",
                    value=record.employee_no or "",
                    reason=(
                        f"employee_no={record.employee_no} 已存在，"
                        f"sync_mode={sync_mode.value} 拒绝"
                    ),
                    error_code="AI_IMPORT_EMPLOYEE_NO_EXISTS",
                )
            )
            continue

        if action in (SyncAction.UPDATE_SAFE, SyncAction.UPDATE_FULL):
            # employee_no 命中：UPDATE_PROFILE / FULL_SYNC 强制 overwrite 语义
            rows_to_overwrite.append((record, existing))
            continue

        # EXISTS_BY_USERNAME：按 on_conflict 处理
        if on_conflict == "skip":
            skipped_count += 1
        elif on_conflict == "fail_fast":
            failed_rows.append(
                FailedRow(
                    row_num=record.row_num,
                    field="user_name",
                    value=record.user_name,
                    reason=(
                        f"user_name={record.user_name} 已存在，"
                        f"on_conflict=fail_fast 拒绝"
                    ),
                    error_code="AI_IMPORT_USERNAME_DUPLICATE",
                )
            )
        else:  # overwrite
            rows_to_overwrite.append((record, existing))

    if out_of_scope_records:
        rows_to_create = []
        rows_to_overwrite = []

    # 8. 读取系统默认密码。
    hashed_password = ""
    if rows_to_create:
        default_password = await get_default_password(db)
        hashed_password = get_password_hash(default_password)

    # 9. 分块并按行 savepoint 落库。
    # 行级处理统一封装为 (kind, record, existing) 三元组
    rows_to_process: list[tuple[str, UserImportRecord, User | None]] = [
        ("create", r, None) for r in rows_to_create
    ] + [("overwrite", r, u) for r, u in rows_to_overwrite]

    total_chunks = max(
        1, (len(rows_to_process) + USER_IMPORT_CHUNK_SIZE - 1) // USER_IMPORT_CHUNK_SIZE
    )

    aborted_error: Exception | None = None
    remaining_after_abort: list[tuple[str, UserImportRecord, User | None]] = []

    for chunk_start in range(0, len(rows_to_process), USER_IMPORT_CHUNK_SIZE):
        chunk = rows_to_process[chunk_start : chunk_start + USER_IMPORT_CHUNK_SIZE]
        chunk_index = chunk_start // USER_IMPORT_CHUNK_SIZE
        chunk_success = 0
        chunk_failed = 0

        try:
            async with db.begin_nested():  # chunk-level savepoint
                for kind, record, existing in chunk:
                    try:
                        async with db.begin_nested():  # row-level nested savepoint
                            if kind == "create":
                                await _process_create_row(
                                    db,
                                    record,
                                    hashed_password,
                                    authorization_actor,
                                    resolutions_by_row[record.row_num],
                                    has_role_column=effective_has_role_column,
                                    ignored_user_ids=ignored_user_ids,
                                )
                                success_count += 1
                            else:  # overwrite
                                await _process_overwrite_row(
                                    db,
                                    record,
                                    existing,
                                    resolutions_by_row[record.row_num],
                                )
                                overwritten_count += 1
                        chunk_success += 1
                    except (BusinessException, IntegrityError) as e:
                        code = _extract_error_code(e)
                        if code not in RECOVERABLE_ERROR_CODES:
                            # 致命 → 让 chunk savepoint 自动 ROLLBACK
                            raise
                        failed_rows.append(_make_failed_row_from_exc(record, e, code))
                        chunk_failed += 1
        except (AuthorizationException, NotFoundException):
            raise
        except BusinessRuleException as e:
            if e.error_code == "AUTHORIZATION_SNAPSHOT_STALE":
                raise
            aborted_error = e
            remaining_after_abort = rows_to_process[chunk_start:]
            for _kind, record, _existing in remaining_after_abort:
                failed_rows.append(
                    FailedRow(
                        row_num=record.row_num,
                        field="_batch",
                        value="",
                        reason=(
                            f"批量执行中断（chunk {chunk_index}）："
                            f"{type(e).__name__}: {e}"
                        ),
                        error_code="AI_IMPORT_BATCH_ABORTED",
                    )
                )
            break
        except Exception as e:
            # 致命错误：chunk savepoint 已 ROLLBACK；标记当前 chunk 全部 + 后续 chunk 全部为 failed
            aborted_error = e
            remaining_after_abort = (
                rows_to_process[chunk_start:]  # 当前 chunk + 后续 chunk
            )
            for _kind, record, _existing in remaining_after_abort:
                failed_rows.append(
                    FailedRow(
                        row_num=record.row_num,
                        field="_batch",
                        value="",
                        reason=f"批量执行中断（chunk {chunk_index}）：{type(e).__name__}: {e}",
                        error_code="AI_IMPORT_BATCH_ABORTED",
                    )
                )
            break

        # 写 CHUNK_PROGRESS 日志。
        await _write_batch_log(
            db,
            batch,
            current_user,
            event="CHUNK_PROGRESS",
            detail={
                "chunk_index": chunk_index,
                "total_chunks": total_chunks,
                "success_in_chunk": chunk_success,
                "failed_in_chunk": chunk_failed,
            },
        )

    # 10. 写失败行文件。
    failed_rows_file: str | None = None
    if failed_rows:
        xlsx_bytes = _failed_rows_to_xlsx_bytes(failed_rows)
        failed_rows_file = await storage.save(
            xlsx_bytes,
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            namespace="import-error",
            suffix=".xlsx",
        )

    # 11. 根据执行结果进入终态。
    end_status = _determine_end_status(
        success_count + overwritten_count,
        len(failed_rows),
    )
    finished_at = datetime.now()

    # CAS RUNNING → end_status
    await _transition_batch_status(
        db,
        batch.batch_id,
        ImportBatchStatus.RUNNING,
        end_status,
        success_count=success_count,
        skipped_count=skipped_count,
        overwritten_count=overwritten_count,
        failed_count=len(failed_rows),
        failed_rows_file=failed_rows_file,
        finished_at=finished_at,
    )
    await db.refresh(batch)  # 同步 ORM 实例（failed_rows_file / status 等）

    # 12. 写 EXECUTE_FINISH 日志。
    await _write_batch_log(
        db,
        batch,
        current_user,
        event="EXECUTE_FINISH",
        from_status=ImportBatchStatus.RUNNING,
        to_status=end_status,
        detail={
            "success_count": success_count,
            "skipped_count": skipped_count,
            "overwritten_count": overwritten_count,
            "failed_count": len(failed_rows),
            "finished_at": finished_at.isoformat(),
            "duration_ms": int((finished_at - started_at).total_seconds() * 1000),
            **({"aborted": str(aborted_error)} if aborted_error else {}),
        },
    )

    return ImportResult(
        batch_id=batch.batch_id,
        status=end_status.value,
        success_count=success_count,
        skipped_count=skipped_count,
        overwritten_count=overwritten_count,
        failed_count=len(failed_rows),
        failed_rows_file=failed_rows_file,
        failed_rows_preview=failed_rows[:FAILED_ROWS_PREVIEW_LIMIT],
        idempotent_replay=False,
    )


def _make_failed_row_from_exc(
    record: UserImportRecord,
    exc: Exception,
    code: str,
) -> FailedRow:
    """从异常构造 FailedRow，按 error_code 决定 field/value。"""
    if code == "AI_IMPORT_USERNAME_DUPLICATE":
        field, value = "user_name", record.user_name
        reason = f"用户名 {record.user_name} 已存在（可能并发导入）"
    elif code == "AI_IMPORT_EMPLOYEE_NO_DUPLICATE":
        field, value = "employee_no", record.employee_no or ""
        reason = f"员工工号 {record.employee_no} 已存在"
    elif code == "AI_IMPORT_EMPLOYEE_NO_EXISTS":
        field, value = "employee_no", record.employee_no or ""
        reason = str(exc)
    elif code in (
        "AI_IMPORT_DEPT_NOT_FOUND",
        "AI_IMPORT_DEPT_PATH_NOT_FOUND",
        "AI_IMPORT_DEPT_DUPLICATE",
        "AI_IMPORT_DEPT_OUT_OF_SCOPE",
    ):
        field, value = "dept_input", record.dept_input
        reason = str(exc)
    elif code in (
        "AI_IMPORT_ROLE_NOT_FOUND",
        "AI_IMPORT_ROLE_OUT_OF_SCOPE",
    ):
        field, value = "role_input", record.role_input or ""
        reason = str(exc)
    else:
        field, value = "_unknown", ""
        reason = str(exc)
    return FailedRow(
        row_num=record.row_num,
        field=field,
        value=value,
        reason=reason,
        error_code=code,
    )


async def get_batch_detail(
    db: AsyncSession,
    batch_id: str,
) -> tuple[UserImportBatch | None, str | None]:
    """按 batch_id 查询批次详情和操作人 user_name。

    一次性 outerjoin sys_user 拿 operator_name，避免 N+1。

    Args:
        db: 异步数据库会话
        batch_id: 批次 ID（UUID 字符串）

    Returns:
        ``(batch, operator_name)``：
        - batch 找不到时 → ``(None, None)``，API 层抛 ``AI_IMPORT_BATCH_NOT_FOUND``
        - operator 用户的 user_name；user 被删除时 → ``None``（outerjoin）
    """
    from app.modules.system.models.user import User  # noqa: PLC0415

    stmt = (
        select(UserImportBatch, User.user_name)
        .outerjoin(User, User.user_id == UserImportBatch.operator_id)
        .where(UserImportBatch.batch_id == batch_id)
    )
    row = (await db.execute(stmt)).first()
    if row is None:
        return None, None
    batch, operator_name = row
    return batch, operator_name


async def list_batch_logs(
    db: AsyncSession,
    batch_id: str,
    *,
    event: str | None = None,
    current: int = 1,
    size: int = 10,
) -> tuple[list[tuple[UserImportBatchLog, str | None]], int]:
    """按 batch_id 分页查询批次日志。

    使用 ``(batch_id, created_at)`` 索引并按 created_at ASC
    排序返回完整状态转换历史（CREATED → PREVIEW_DONE → EXECUTE_START →
    CHUNK_PROGRESS * N → EXECUTE_FINISH）。

    outerjoin sys_user 拿 operator_name（同 ``get_batch_detail``）：user 删除时
    返回 ``None``，日志行仍保留，优先保证审计完整性。

    Args:
        db: 异步数据库会话
        batch_id: 批次 ID
        event: 可选事件类型过滤（CREATED/PREVIEW_DONE/EXECUTE_START/CHUNK_PROGRESS/
            EXECUTE_FINISH/EXECUTE_FAILED/EXPIRED/CANCELLED）。未知事件值返回空列表，
            不额外引入请求校验错误。
        current: 页码（1-based）
        size: 每页数量

    Returns:
        ``(rows, total)``：
        - ``rows`` 是 ``[(log, operator_name), ...]`` 元组列表
        - ``total`` 是过滤后总条数（用于前端分页器）
    """
    from app.modules.system.models.user import User  # noqa: PLC0415

    filters: list = [UserImportBatchLog.batch_id == batch_id]
    if event:
        filters.append(UserImportBatchLog.event == event)

    count_stmt = select(func.count()).select_from(UserImportBatchLog).where(*filters)
    total = (await db.execute(count_stmt)).scalar() or 0

    stmt = (
        select(UserImportBatchLog, User.user_name)
        .outerjoin(User, User.user_id == UserImportBatchLog.operator_id)
        .where(*filters)
        .order_by(
            UserImportBatchLog.created_at.asc(),
            UserImportBatchLog.log_id.asc(),
        )
        .offset((current - 1) * size)
        .limit(size)
    )
    rows = (await db.execute(stmt)).all()

    return rows, total


async def list_batches(
    db: AsyncSession,
    query: UserImportBatchQuery,
) -> tuple[list[tuple[UserImportBatch, str | None]], int]:
    """分页查询导入批次列表。

    支持 ``operator_id`` / ``status`` / ``created_at`` 时间窗过滤；按
    ``(created_at DESC, batch_id DESC)`` 排序保证分页稳定（同秒批次按 batch_id
    字典序降序兜底，避免 tie-flicker）。

    outerjoin sys_user 拿 operator_name（同 ``get_batch_detail`` / ``list_batch_logs``）：
    user 删除时返 ``None``，batch 行保留（审计完整性 > 引用完整性）。

    Args:
        db: 异步数据库会话
        query: 分页 + 过滤参数（current/size/operator_id/status/start_time/end_time）

    Returns:
        ``(rows, total)``：
        - ``rows`` 是 ``[(batch, operator_name), ...]`` 元组列表
        - ``total`` 是过滤后总条数（前端分页器用）

    Raises:
        BusinessRuleException: ``AI_IMPORT_INVALID_STATUS`` — status 非合法枚举值
    """
    from app.modules.system.models.user import User  # noqa: PLC0415

    filters: list = []
    if query.operator_id is not None:
        filters.append(UserImportBatch.operator_id == query.operator_id)
    if query.status:
        try:
            status_enum = ImportBatchStatus(query.status)
        except ValueError as e:
            raise BusinessRuleException(
                f"非法 status 值：{query.status}",
                error_code="AI_IMPORT_INVALID_STATUS",
            ) from e
        filters.append(UserImportBatch.status == status_enum)
    if query.start_time is not None:
        filters.append(UserImportBatch.created_at >= query.start_time)
    if query.end_time is not None:
        filters.append(UserImportBatch.created_at <= query.end_time)

    count_stmt = select(func.count()).select_from(UserImportBatch).where(*filters)
    total = (await db.execute(count_stmt)).scalar() or 0

    stmt = (
        select(UserImportBatch, User.user_name)
        .outerjoin(User, User.user_id == UserImportBatch.operator_id)
        .where(*filters)
        .order_by(
            UserImportBatch.created_at.desc(),
            UserImportBatch.batch_id.desc(),
        )
        .offset((query.current - 1) * query.size)
        .limit(query.size)
    )
    rows = (await db.execute(stmt)).all()

    return rows, total


#: 协作式取消标志前缀，由 chunk loop 检查。
_CANCEL_REDIS_PREFIX = "user_import:cancel:"

#: 取消标志保留 1 小时，覆盖正常 chunk 执行窗口。
_CANCEL_REDIS_TTL_SECONDS = 3600


async def cancel_batch(
    db: AsyncSession,
    batch_id: str,
    operator: User,
    reason: str,
    *,
    file_storage: FileStorage | None = None,
) -> UserImportBatch:
    """取消导入批次。

    两种取消场景：

    - **PREVIEW_DONE → CANCELLED**：
      CAS 直接转 CANCELLED + 写 batch_log（event=CANCELLED）+ 删除 preview 文件
      + 删除 Redis preview cache。dry_run 完成态的批次可以无损取消（数据未落库）。
    - **RUNNING 协作式取消**：
      设置 Redis 标志 ``user_import:cancel:{batch_id}``（TTL 1h），立即返回。
      chunk loop 在下一个 chunk 边界检测到标志 → break → 状态转 PARTIAL_SUCCESS
      （已 commit 的 chunk 保留，无法回滚）。

    Args:
        db: 异步数据库会话（API 层 commit）
        batch_id: 批次 ID
        operator: 操作人（必须是 batch.operator_id 本人或超管）
        reason: 业务理由（API 层 ReasonSchema 已校验，service 层 defense-in-depth）
        file_storage: 可选注入（测试用）

    Returns:
        更新后的 ``UserImportBatch``：

        - 场景 1：``status=CANCELLED`` + ``finished_at`` 已设置
        - 场景 2：``status=RUNNING``（未变），调用方需提示前端「已请求取消，
          正在等待当前 chunk 完成」

    Raises:
        NotFoundException: batch_id 不存在（``AI_IMPORT_BATCH_NOT_FOUND``）
        AuthorizationException: 非操作人本人且非超管
        UnprocessableEntityException: 状态不可取消（CREATED / SUCCESS /
            PARTIAL_SUCCESS / FAILED / EXPIRED / CANCELLED，
            ``AI_IMPORT_BATCH_NOT_CANCELLABLE``）
    """
    reason = _validate_reason(reason)

    batch = await db.get(UserImportBatch, batch_id)
    if batch is None:
        raise NotFoundException(
            "用户导入批次",
            error_code="AI_IMPORT_BATCH_NOT_FOUND",
        )

    # 仅操作人本人或超管可取消。
    from app.core.rbac import is_super_admin  # noqa: PLC0415

    if batch.operator_id != operator.user_id and not is_super_admin(operator):
        raise AuthorizationException("无权取消此批次")

    now = datetime.now()

    if batch.status == ImportBatchStatus.PREVIEW_DONE:
        # PREVIEW_DONE 直接转 CANCELLED；CAS 防止 execute 与 cancel 并发覆盖。
        ok = await _transition_batch_status(
            db,
            batch_id,
            ImportBatchStatus.PREVIEW_DONE,
            ImportBatchStatus.CANCELLED,
            finished_at=now,
        )
        if not ok:
            # CAS 失败：并发 execute 已把 status 改成 RUNNING，或并发 cancel 已抢
            raise UnprocessableEntityException(
                "批次状态已变化，无法取消",
                error_code="AI_IMPORT_BATCH_NOT_CANCELLABLE",
            )

        # 同步 ORM 实例状态（_transition_batch_status 绕过 synchronize_session）
        batch.status = ImportBatchStatus.CANCELLED
        batch.finished_at = now
        await db.flush()

        # 写入 CANCELLED 审计日志。
        await _write_batch_log(
            db,
            batch,
            operator,
            event="CANCELLED",
            from_status=ImportBatchStatus.PREVIEW_DONE,
            to_status=ImportBatchStatus.CANCELLED,
            detail={
                "reason": reason,
                "cancelled_by": operator.user_id,
                "scenario": "preview_done_direct_cancel",
            },
        )

        # 清理预览文件和 Redis 缓存。
        storage = file_storage or get_file_storage()
        try:
            await storage.delete(batch.file_storage_key)
        except FileNotFoundError:
            # 文件已被 cleanup cron 清理 / never existed — 不影响 cancel 主流程
            pass
        await redis_module.redis_client.delete(
            f"{_PREVIEW_REDIS_PREFIX}{batch.preview_token}"
        )

        return batch

    if batch.status == ImportBatchStatus.RUNNING:
        # 场景 2：协作式 cancel — 设置 Redis 标志，chunk loop 检测后 break
        # RUNNING 由 chunk loop 协作检查取消标志。
        await redis_module.redis_client.setex(
            f"{_CANCEL_REDIS_PREFIX}{batch_id}",
            _CANCEL_REDIS_TTL_SECONDS,
            "1",
        )
        # batch.status 保持 RUNNING，actual transition 在 chunk loop 内发生
        # API 层根据返回的 batch.status（RUNNING）告知前端「已请求取消」
        return batch

    # CREATED / SUCCESS / PARTIAL_SUCCESS / FAILED / EXPIRED / CANCELLED → 拒绝
    # 其他状态不可取消。
    raise UnprocessableEntityException(
        "批次状态不可取消",
        error_code="AI_IMPORT_BATCH_NOT_CANCELLABLE",
    )


async def cleanup_expired_batches(db: AsyncSession) -> int:
    """清理 90 天前终态批次及其失败清单和预览文件。

    每日 02:00 cron 入口（``app.tasks.user_cleanup_tasks.clean_expired_import_batches``）。
    本函数不 commit，由 task wrapper / API 层负责。

    Returns:
        删除的 batch 行数（含 CASCADE 自动删的 batch_log）

    安全边界：
    - 只删 ``TERMINAL_STATUSES`` 内的 batch（SUCCESS / PARTIAL_SUCCESS / FAILED /
      EXPIRED / CANCELLED）；CREATED / PREVIEW_DONE / RUNNING 不动：
        - CREATED 90 天前不可能存在（dry_run 即时转入 PREVIEW_DONE）
        - PREVIEW_DONE 应由 ``cleanup_expired_previews`` 处理
        - RUNNING 90 天前是 zombie，归 ``JobLogMonitor`` 类孤儿监控
    - failed_rows_file / file_storage_key 缺失（None 或文件已被外部删）不抛错，
      继续删 DB 行（防 dangling file 阻塞 cleanup）
    """
    cutoff = datetime.now() - timedelta(days=90)
    fs = get_file_storage()
    terminal_values = [s.value for s in TERMINAL_STATUSES]

    stmt = select(UserImportBatch).where(
        UserImportBatch.created_at < cutoff,
        UserImportBatch.status.in_(terminal_values),
    )
    batches = (await db.execute(stmt)).scalars().all()

    for batch in batches:
        if batch.failed_rows_file:
            try:
                await fs.delete(batch.failed_rows_file)
            except FileNotFoundError:
                pass
        if batch.file_storage_key:
            try:
                await fs.delete(batch.file_storage_key)
            except FileNotFoundError:
                pass
        await db.delete(batch)
        # batch_log FK ondelete=CASCADE 自动跟着删（DB 层触发，flush 后生效）

    if batches:
        # 触发 DELETE SQL + CASCADE，让调用方查询时数据一致
        await db.flush()

    return len(batches)


async def cleanup_expired_previews(db: AsyncSession) -> int:
    """将超过 10 分钟的 PREVIEW_DONE 批次标记为 EXPIRED 并清理文件。

    每小时 cron 入口（``app.tasks.user_cleanup_tasks.clean_expired_import_previews``）。
    本函数不 commit。

    Returns:
        标记为 EXPIRED 的 batch 行数

    设计要点：
    - 用 CAS ``_transition_batch_status(PREVIEW_DONE → EXPIRED)`` 防并发覆盖：
      用户在 10min 边界刚 cancel / 刚 execute 时，CAS rowcount=0，本函数跳过不报错
    - 删 file_storage_key 用 ``try/except FileNotFoundError`` 兜底（cancel 流程
      可能已删过；双重删除不抛错）
    - 写入 EXPIRED 批次日志，
      operator_id 用 batch.operator_id（系统触发但归属原操作人，便于审计反查）
    """
    cutoff = datetime.now() - timedelta(minutes=10)
    fs = get_file_storage()

    stmt = select(UserImportBatch).where(
        UserImportBatch.status == ImportBatchStatus.PREVIEW_DONE,
        UserImportBatch.created_at < cutoff,
    )
    batches = (await db.execute(stmt)).scalars().all()

    expired_count = 0
    for batch in batches:
        success = await _transition_batch_status(
            db,
            batch.batch_id,
            ImportBatchStatus.PREVIEW_DONE,
            ImportBatchStatus.EXPIRED,
            finished_at=datetime.now(),
        )
        if not success:
            continue

        if batch.file_storage_key:
            try:
                await fs.delete(batch.file_storage_key)
            except FileNotFoundError:
                pass

        db.add(
            UserImportBatchLog(
                log_id=str(next_id()),
                batch_id=batch.batch_id,
                operator_id=batch.operator_id,
                event="EXPIRED",
                from_status=ImportBatchStatus.PREVIEW_DONE,
                to_status=ImportBatchStatus.EXPIRED,
                detail={"reason": "preview TTL 10min expired (cleanup cron)"},
            )
        )
        await db.flush()
        expired_count += 1

    return expired_count


__all__ = [
    "batch_create_users_from_records",
    "cancel_batch",
    "cleanup_expired_batches",
    "cleanup_expired_previews",
    "dry_run_import_users",
    "get_batch_by_preview_token",
    "get_batch_detail",
    "list_batches",
    "list_batch_logs",
]
