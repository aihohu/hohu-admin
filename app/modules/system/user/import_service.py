"""ImportService 主流程（v2.2 P0/P1）。

Task 9：dry_run_import_users（spec §3.6 line 2049-2066）

职责：
- 接收已 parse 的 records（UserImportRecord 列表），跑四象限分类：
  - new_records：resolve_existing_user 返回 (None, _)
  - exists_records：(user, matched=False) 按 user_name 命中
  - conflict_records：dept/role 反查失败（per-row）
  - out_of_scope_records：Permission Boundary + Data Scope 越界（per-row）
- 算 file_sha256 + records_hash + preview_token
- INSERT sys_user_import_batch（CREATED → flush → UPDATE PREVIEW_DONE；事务由调用方控制）
- Redis cache preview_token → batch_id（10min TTL，spec §2.19 v2.2 P0）
- records 截断到 MAX_PREVIEW_RECORDS（spec §3.2 v2.2 P1）

execute（Task 10）/ parse（已 Task 8 在 import_parser.py）/ export（Task 11）后续补。
"""

import hashlib
import json
import secrets
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis as redis_module
from app.core.exceptions import BusinessRuleException
from app.modules.system.user.constants import (
    MAX_PREVIEW_RECORDS,
    ImportBatchStatus,
)
from app.modules.system.user.import_state import validate_transition
from app.modules.system.user.import_validator import (
    check_dept_data_scope,
    check_permission_boundary,
    resolve_dept,
    resolve_existing_user,
    resolve_role_input,
)
from app.modules.system.user.models import UserImportBatch
from app.modules.system.user.schemas import (
    FailedRow,
    ImportDryRunResult,
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


__all__ = [
    "dry_run_import_users",
    "get_batch_by_preview_token",
]
