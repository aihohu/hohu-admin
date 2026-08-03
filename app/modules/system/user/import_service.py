"""ImportService 主流程（v2.2 P0/P1）。

Task 9：dry_run_import_users（spec §3.6 line 2049-2066）
Task 10：batch_create_users_from_records（spec §3.6 line 2068-2097）

职责：
- dry_run_import_users：四象限分类 + preview_token + Redis cache（详见 Task 9）
- batch_create_users_from_records：
  - preview_token 三重校验（file_sha256 + records_hash + operator_id，spec §2.19）
  - CAS PREVIEW_DONE → RUNNING（spec §2.27 幂等核心）
  - chunk 100 rows + 行级 savepoint（spec §2.20）
  - IntegrityError 区分 user_name UNIQUE → AI_IMPORT_USERNAME_DUPLICATE（spec §2.25）
  - on_conflict skip / overwrite / fail_fast（spec §2.21）
  - 写 batch_log（EXECUTE_START / CHUNK_PROGRESS / EXECUTE_FINISH，spec §2.28）
  - failed_rows 文件化（spec §3.3）

parse（已 Task 8 在 import_parser.py）/ export（Task 11）后续补。
"""

import hashlib
import io
import json
import secrets
from datetime import datetime
from typing import Literal

from openpyxl import Workbook
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis as redis_module
from app.core.exceptions import BusinessException, BusinessRuleException
from app.core.file_storage import FileStorage, get_file_storage
from app.core.id_generator import next_id
from app.core.security import get_password_hash
from app.db.base import user_depts, user_roles
from app.modules.system.models.user import User
from app.modules.system.user.constants import (
    FAILED_ROWS_PREVIEW_LIMIT,
    MAX_PREVIEW_RECORDS,
    OVERWRITE_ALLOWED,
    RECOVERABLE_ERROR_CODES,
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
    check_dept_data_scope,
    check_permission_boundary,
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
    UserImportRecord,
)

#: Redis key 前缀 + TTL（spec line 534 + 557：10min cache only）
_PREVIEW_REDIS_PREFIX = "user_import:preview:"
_PREVIEW_REDIS_TTL_SECONDS = 600

#: batch_id / preview_token 生成长度
_BATCH_ID_LENGTH = 32
_PREVIEW_TOKEN_LENGTH = 32


def _generate_batch_id() -> str:
    """UUID-style batch_id（spec §3.6 line 1749）。"""
    return secrets.token_urlsafe(_BATCH_ID_LENGTH)[:_BATCH_ID_LENGTH]


def _generate_preview_token() -> str:
    """preview_token：URL-safe 随机串（spec §2.19）。"""
    return secrets.token_urlsafe(_PREVIEW_TOKEN_LENGTH)[:_PREVIEW_TOKEN_LENGTH]


def _compute_file_sha256(file_bytes: bytes) -> str:
    """file_sha256：execute 三重校验用（spec §2.19 line 575）。"""
    return hashlib.sha256(file_bytes).hexdigest()


def _compute_records_hash(records: list[UserImportRecord]) -> str:
    """records_hash：序列化 records 为 sorted JSON 后 sha256（spec §2.19 line 577）。

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
    """reason 入口校验（spec §2.30 v2.2 P1-3）。

    API 层 ReasonSchema 已校验，service 层 defense-in-depth（AI tool 直接调用时
    也能拦住）。strip 后 1-256 字符；不通过抛 AI_IMPORT_REASON_REQUIRED。
    """
    if reason is None:
        raise BusinessRuleException(
            "reason 必填（spec §2.30）",
            error_code="AI_IMPORT_REASON_REQUIRED",
        )
    stripped = reason.strip()
    if not stripped or len(stripped) > 256:
        raise BusinessRuleException(
            "reason 必填且长度 1-256 字符（spec §2.30）",
            error_code="AI_IMPORT_REASON_REQUIRED",
        )
    return stripped


async def _classify_records(
    db: AsyncSession,
    records: list[UserImportRecord],
    current_user,
) -> tuple[
    list[UserImportRecord],
    list[UserImportRecord],
    list[FailedRow],
    list[FailedRow],
]:
    """四象限分类核心逻辑（spec §3.6 line 2060-2062）。

    顺序：先做越界集合校验（一次性算所有行的 out_of_scope），再逐行做 conflict
    反查；剩余行按 resolve_existing_user 命中分 new / exists。

    Returns:
        (new_records, exists_records, conflict_records, out_of_scope_records)
    """
    # 1. 集合级权限校验：一次性返回所有 out_of_scope 行
    role_oos = await check_permission_boundary(db, records, current_user)
    dept_oos = await check_dept_data_scope(db, records, current_user)
    oos_row_nums = {f.row_num for f in role_oos} | {f.row_num for f in dept_oos}

    new_records: list[UserImportRecord] = []
    exists_records: list[UserImportRecord] = []
    conflict_records: list[FailedRow] = []

    # 2. 逐行：跳过 oos 行，反查 dept/role，命中已存在则 exists 否则 new
    for record in records:
        if record.row_num in oos_row_nums:
            continue  # 已在 out_of_scope 集合，不重复进 conflict

        # dept 反查（role_input 在 check_permission_boundary 已反查过；
        # 此处仅校验 dept 存在性，role 已确认有效）
        try:
            await resolve_dept(db, record.dept_input)
        except BusinessRuleException as exc:
            conflict_records.append(
                FailedRow(
                    row_num=record.row_num,
                    field="dept_input",
                    value=record.dept_input,
                    reason=str(exc),
                    error_code=exc.error_code,
                )
            )
            continue

        # role_input 未在 permission_boundary 中反查（超管豁免路径下不会查）
        # 这里再做一次确保 conflict 集合完整（超管分配未存在角色也归 conflict）
        if record.role_input:
            try:
                await resolve_role_input(db, record.role_input)
            except BusinessRuleException as exc:
                conflict_records.append(
                    FailedRow(
                        row_num=record.row_num,
                        field="role_input",
                        value=record.role_input,
                        reason=str(exc),
                        error_code="AI_IMPORT_ROLE_NOT_FOUND",
                    )
                )
                continue

        # 命中已存在：spec §2.24 line 874，按 user_name 命中归 exists
        existing, _matched_by_emp = await resolve_existing_user(db, record)
        if existing is not None:
            exists_records.append(record)
        else:
            new_records.append(record)

    return new_records, exists_records, conflict_records, role_oos + dept_oos


async def dry_run_import_users(
    db: AsyncSession,
    records: list[UserImportRecord],
    current_user,
    file_bytes: bytes,
    filename: str,
    reason: str,
    *,
    on_conflict: Literal["skip", "overwrite", "fail_fast"] = "skip",
) -> tuple[ImportDryRunResult, UserImportBatch]:
    """预检 + 生成 preview_token（spec §3.6 line 2049-2066）。

    流程：
    1. 入口校验 reason（spec §2.30）
    2. 算 file_sha256 / records_hash / preview_token / batch_id
    3. INSERT sys_user_import_batch (status=CREATED)
    4. 四象限分类（new / exists / conflict / out_of_scope）
    5. 截断每个象限到 MAX_PREVIEW_RECORDS（spec §3.2）
    6. CAS 状态 CREATED → PREVIEW_DONE（validate_transition 校验合法）
    7. Redis cache preview_token → batch_id（10min TTL，spec §2.19）
    8. 返回 (ImportDryRunResult, batch)，调用方拿 preview_token 给前端

    Notes:
        - Service 层不 commit（API 层负责）；outer-transaction rollback 时 INSERT 自动撤销
        - Redis cache 即使后续丢失，execute 仍可凭 preview_token 反查 DB（spec §2.19 反例 2）

    Raises:
        BusinessRuleException: ``AI_IMPORT_REASON_REQUIRED`` — reason 缺失或超长
    """
    reason_clean = _validate_reason(reason)

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

    # 2. 四象限分类
    (
        new_records,
        exists_records,
        conflict_records,
        out_of_scope_records,
    ) = await _classify_records(db, records, current_user)

    # 3. 截断（spec §3.2 v2.2 P1）
    new_truncated_list, new_trunc = _truncate(new_records)
    exists_truncated_list, exists_trunc = _truncate(exists_records)
    conflict_truncated_list, conflict_trunc = _truncate(conflict_records)
    oos_truncated_list, oos_trunc = _truncate(out_of_scope_records)

    # 4. UPDATE batch summary + status=PREVIEW_DONE（spec line 2063）
    validate_transition(ImportBatchStatus.CREATED, ImportBatchStatus.PREVIEW_DONE)
    batch.summary_new = len(new_records)
    batch.summary_exists = len(exists_records)
    batch.summary_conflict = len(conflict_records)
    batch.summary_out_of_scope = len(out_of_scope_records)
    batch.status = ImportBatchStatus.PREVIEW_DONE
    await db.flush()

    # 5. Redis cache（spec §2.19 v2.2 P0：仅 cache batch_id）
    cache_payload = json.dumps({"batch_id": batch_id})
    await redis_module.redis_client.setex(
        f"{_PREVIEW_REDIS_PREFIX}{preview_token}",
        _PREVIEW_REDIS_TTL_SECONDS,
        cache_payload,
    )

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
    """凭 preview_token 反查 batch（spec §2.19 v2.2 P0：先 Redis 后 DB）。

    Task 10 execute 阶段使用；本模块集中管理 cache fallback 逻辑。
    """
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
            # spec §2.19 反例 2：Redis 命中但 batch 不存在（脏数据）→ fall through 到 DB 反查

    # 2. DB 反查（cache miss / 脏数据 fallback）
    return (
        await db.execute(
            select(UserImportBatch).where(
                UserImportBatch.preview_token == preview_token
            )
        )
    ).scalar_one_or_none()


# ============================================================================
# Task 10: batch_create_users_from_records（spec §3.6 line 2068-2097）
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
    """UNIQUE IntegrityError → 业务 error_code（spec §2.25 line 921-932）。

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
    """写一行 batch_log（spec §2.28）。

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
    """生成 failed_rows Excel 文件 bytes（spec §3.3 line 1700 + §5.4）。

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
    """execute 完成后状态判定（spec §2.26 line 970-976）。

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
) -> None:
    """新建用户行级处理（INSERT user + bind roles + bind dept）。

    失败抛异常 → 上层 savepoint 自动 ROLLBACK。
    """
    dept_id = await resolve_dept(db, record.dept_input)
    role_ids: list[int] = []
    if record.role_input:
        role_ids = await resolve_role_input(db, record.role_input)

    new_user = User(
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

    # bind roles / depts via core insert（避免 ORM 二次 SELECT）
    if role_ids:
        await db.execute(
            insert(user_roles),
            [{"user_id": new_user.user_id, "role_id": rid} for rid in role_ids],
        )
    await db.execute(
        insert(user_depts),
        [{"user_id": new_user.user_id, "dept_id": dept_id, "is_primary": "Y"}],
    )
    await db.flush()  # 触发 bind INSERT


async def _process_overwrite_row(
    db: AsyncSession,
    record: UserImportRecord,
    existing: User,
) -> None:
    """overwrite 已存在用户：仅更新 OVERWRITE_ALLOWED 字段（spec §2.21）。

    user_name / hashed_password / user_id / create_time 永不覆盖。
    """
    dept_id = await resolve_dept(db, record.dept_input)
    role_ids: list[int] = []
    if record.role_input:
        role_ids = await resolve_role_input(db, record.role_input)

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

    # 覆盖 roles / dept（全量重置，spec §2.21 line 740-744）
    if role_ids:
        await db.execute(
            user_roles.delete().where(user_roles.c.user_id == existing.user_id)
        )
        await db.execute(
            insert(user_roles),
            [{"user_id": existing.user_id, "role_id": rid} for rid in role_ids],
        )

    await db.execute(
        user_depts.delete().where(user_depts.c.user_id == existing.user_id)
    )
    await db.execute(
        insert(user_depts),
        [{"user_id": existing.user_id, "dept_id": dept_id, "is_primary": "Y"}],
    )
    await db.flush()


async def _handle_idempotent_replay(
    db: AsyncSession,
    batch_id: str,
) -> ImportResult:
    """CAS 失败后查最新 batch 行，按状态返回幂等响应或抛异常（spec §2.27 line 1205-1210）。

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
        raise BusinessRuleException(
            "preview_token 无效或已过期",
            error_code="AI_IMPORT_PREVIEW_INVALID",
        )

    if fresh.status in (ImportBatchStatus.SUCCESS, ImportBatchStatus.PARTIAL_SUCCESS):
        return ImportResult(
            batch_id=fresh.batch_id,
            success_count=fresh.success_count,
            skipped_count=fresh.skipped_count,
            overwritten_count=fresh.overwritten_count,
            failed_count=fresh.failed_count,
            failed_rows_file=fresh.failed_rows_file,
            idempotent_replay=True,
        )
    if fresh.status == ImportBatchStatus.RUNNING:
        raise BusinessRuleException(
            "批次正在执行中，请等待",
            error_code="AI_IMPORT_BATCH_RUNNING",
        )
    # FAILED / EXPIRED / CANCELLED → 不能重放
    raise BusinessRuleException(
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
) -> ImportResult:
    """批量新建 / 更新（spec §3.6 line 2068-2097）。

    流程（#2.19-2.22 + #2.25-2.28 全套）：
    1. 凭 preview_token 反查 batch（Redis 加速 + DB SoT，spec §2.19）
    2. reason 一致性校验（preview vs execute，spec §2.30）
    3. 三重校验：file_sha256 + records_hash + operator_id 一致（spec §2.19 line 575-577）
    4. CAS PREVIEW_DONE → RUNNING（spec §2.27 幂等核心）
       - CAS 失败 → 幂等重放或抛错（详见 _handle_idempotent_replay）
    5. dry_run 二次跑分类（防 dry_run 后数据变化）→ conflict + oos 直接进 failed_rows
    6. exists_records 按 sync_mode + on_conflict 分流：skipped / failed / overwrite
    7. chunk 100 rows（spec §2.20）：行级 savepoint + 可恢复错误白名单
       - IntegrityError 区分 user_name UNIQUE → AI_IMPORT_USERNAME_DUPLICATE（spec §2.25）
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
    """
    storage = file_storage or get_file_storage()
    reason_clean = _validate_reason(reason)

    # 1. 反查 batch
    batch = await get_batch_by_preview_token(db, preview_token)
    if batch is None:
        raise BusinessRuleException(
            "preview_token 无效或已过期",
            error_code="AI_IMPORT_PREVIEW_INVALID",
        )

    # 2. reason 一致性（spec §2.30）
    validate_reason_consistency(batch.reason, reason_clean)

    # 3. 三重校验（spec §2.19 line 575-577）
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

    # 4. CAS PREVIEW_DONE → RUNNING（spec §2.27 幂等核心）
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

    # 5. EXECUTE_START log（spec §2.28 line 1256）
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

    # 6. 二次跑分类（防 dry_run 后数据变化）
    (
        new_records,
        exists_records,
        conflict_records,
        out_of_scope_records,
    ) = await _classify_records(db, records, current_user)
    failed_rows: list[FailedRow] = list(conflict_records) + list(out_of_scope_records)
    skipped_count = 0
    overwritten_count = 0
    success_count = 0

    # 7. exists_records 按 sync_mode + on_conflict 分流
    rows_to_create: list[UserImportRecord] = list(new_records)
    rows_to_overwrite: list[tuple[UserImportRecord, User]] = []

    for record in exists_records:
        existing, matched_by_emp = await resolve_existing_user(db, record)
        action = classify_sync_action(matched_by_emp, sync_mode)

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

    # 8. 默认密码（spec §2.5）
    default_password = await get_default_password(db)
    hashed_password = get_password_hash(default_password)

    # 9. chunk + savepoint 落库（spec §2.20）
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
                                await _process_create_row(db, record, hashed_password)
                                success_count += 1
                            else:  # overwrite
                                await _process_overwrite_row(db, record, existing)
                                overwritten_count += 1
                        chunk_success += 1
                    except (BusinessException, IntegrityError) as e:
                        code = _extract_error_code(e)
                        if code not in RECOVERABLE_ERROR_CODES:
                            # 致命 → 让 chunk savepoint 自动 ROLLBACK
                            raise
                        failed_rows.append(_make_failed_row_from_exc(record, e, code))
                        chunk_failed += 1
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

        # CHUNK_PROGRESS log（spec §2.28 line 1257）
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

    # 10. 写 failed_rows xlsx 文件（spec §3.3 + §2.22）
    failed_rows_file: str | None = None
    if failed_rows:
        xlsx_bytes = _failed_rows_to_xlsx_bytes(failed_rows)
        failed_rows_file = await storage.save(
            xlsx_bytes,
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            namespace="import-error",
            suffix=".xlsx",
        )

    # 11. 状态转 SUCCESS / PARTIAL_SUCCESS / FAILED（spec §2.26）
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

    # 12. EXECUTE_FINISH log（spec §2.28 line 1258）
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


__all__ = [
    "batch_create_users_from_records",
    "dry_run_import_users",
    "get_batch_by_preview_token",
]
