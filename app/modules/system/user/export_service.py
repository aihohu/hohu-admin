"""ExportService 导出（v2.2 P0/P1）。

Task 11：export_users_to_excel（spec §3.6 line 2099-2111 + §2.31 P1-5 line 1436-1614）

职责：
- 强制建 UserExportTask（即使同步路径也建，spec §2.31 P1-5 反例 1）
- filter_snapshot 冻结当时的 filter + accessible_dept_ids + filter_evaluated_at
  （spec §2.31 line 1516-1520，防事后部门结构变化导致审计反查漂移）
- reason 必填（spec §2.30 v2.2 P1-3，与 import batch.reason 对称）
- EXPORT_ALLOWED_FIELDS 白名单（hashed_password 永不导出，spec §2.9）
- 行数 > USER_EXPORT_ASYNC_THRESHOLD → AI_EXPORT_ASYNC_REQUIRED（spec §2.6）
- 30 天 TTL 文件存储（spec §2.31 line 1452 / 1554）
- 失败也建 task（status=FAILED + error_code + error_message，spec §2.31 line 1567-1572）

无 batch_log（导出无 chunk 概念，spec §2.31 line 1456-1458）。
异步导出（> USER_EXPORT_ASYNC_THRESHOLD）推迟到 Phase 3。
"""

import io
from datetime import datetime, timedelta

from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import PageResult
from app.core.exceptions import BusinessRuleException, UnprocessableEntityException
from app.core.file_storage import FileStorage, get_file_storage
from app.core.id_generator import next_id
from app.db.base import user_depts
from app.modules.system.models.user import User
from app.modules.system.user.constants import (
    EXPORT_ALLOWED_FIELDS,
    USER_EXPORT_ASYNC_THRESHOLD,
    ExportTaskStatus,
)
from app.modules.system.user.import_validator import _compute_accessible_dept_ids
from app.modules.system.user.models import UserExportTask
from app.modules.system.user.schemas import UserExportFilter, UserExportTaskQuery
from app.utils.data_scope import get_user_data_scope_filters
from app.utils.pagination import paginate

#: Excel 列顺序（spec §2.9 line 266 EXPORT_ALLOWED_FIELDS 顺序）。
#: 字段名 ↔ 中文表头，第 1 项是 ORM 属性 / 派生属性名。
_EXPORT_COLUMN_ORDER: tuple[tuple[str, str], ...] = (
    ("user_name", "账号"),
    ("nickname", "昵称"),
    ("user_email", "邮箱"),
    ("user_phone", "手机号"),
    ("dept_id", "部门ID"),
    ("role_codes", "角色编码"),
    ("user_gender", "性别"),
    ("status", "状态"),
    ("create_time", "创建时间"),
)
# 防御性断言：列顺序必须与 EXPORT_ALLOWED_FIELDS 集合一致，新增敏感字段时
# 这里会立刻报错（spec §2.9 line 268「未列入的字段不进 Excel；新增敏感字段
# 时本白名单不变（默认安全）」）。
assert {col for col, _ in _EXPORT_COLUMN_ORDER} == EXPORT_ALLOWED_FIELDS, (
    "EXPORT_COLUMN_ORDER 必须与 EXPORT_ALLOWED_FIELDS 一致"
)

#: spec §2.31 line 1452 / 1554：30 天 TTL
_EXPORT_FILE_TTL_SECONDS = 30 * 86400

#: spec §2.31 line 1552：namespace
_EXPORT_FILE_NAMESPACE = "user-export"

#: spec §2.31 line 1551：mime
_EXPORT_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _validate_reason(reason: str) -> str:
    """reason 入口校验（spec §2.30 v2.2 P1-3，与 import_service._validate_reason 对称）。

    service 层 defense-in-depth：AI tool 直接调 export_service 时也能拦住。
    """
    if reason is None:
        raise BusinessRuleException(
            "reason 必填（spec §2.30）",
            error_code="AI_EXPORT_REASON_REQUIRED",
        )
    stripped = reason.strip()
    if not stripped or len(stripped) > 256:
        raise BusinessRuleException(
            "reason 必填且长度 1-256 字符（spec §2.30）",
            error_code="AI_EXPORT_REASON_REQUIRED",
        )
    return stripped


async def _query_users_with_data_scope(
    db: AsyncSession,
    filter_: UserExportFilter,
    current_user: User,
) -> list[User]:
    """按 filter + data_scope 查询用户列表（spec §2.31 line 1545）。

    - filter 字段：user_name / nickname / user_email / user_phone / status / dept_id
    - data_scope 自动应用：HR 只能导他可见的部门用户
    - 排序：create_time desc（与 list 接口一致）
    """
    stmt = select(User)

    if filter_.user_name:
        stmt = stmt.where(User.user_name.contains(filter_.user_name))
    if filter_.nickname:
        stmt = stmt.where(User.nickname.contains(filter_.nickname))
    if filter_.user_email:
        stmt = stmt.where(User.user_email.contains(filter_.user_email))
    if filter_.user_phone:
        stmt = stmt.where(User.user_phone.contains(filter_.user_phone))
    if filter_.status:
        stmt = stmt.where(User.status == filter_.status)
    if filter_.dept_id:
        # 多对多：user 在指定 dept 中（user_depts join）
        dept_id_int = int(filter_.dept_id)
        subq = select(user_depts.c.user_id).where(user_depts.c.dept_id == dept_id_int)
        stmt = stmt.where(User.user_id.in_(subq))

    # data_scope（HR 只能导他可见的部门用户）
    scope_filters = await get_user_data_scope_filters(db, current_user)
    for f in scope_filters:
        stmt = stmt.where(f)

    stmt = stmt.order_by(User.create_time.desc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


def _build_excel(rows: list[User]) -> bytes:
    """按 EXPORT_ALLOWED_FIELDS 白名单构造 Excel bytes（spec §2.9 + §3.8）。

    - 列顺序固定（_EXPORT_COLUMN_ORDER）
    - hashed_password 永不导出（spec §2.9 反例 1）
    - role_codes：逗号分隔 role_code（仅启用角色）
    - dept_id：第一个 dept 的 id（多部门场景导出主部门即可，反查场景按 dept_id 单独查）
    - create_time：YYYY-MM-DD HH:MM:SS 格式（防 Excel 时区漂移）
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "users"

    # 表头（中文，便于管理员直接读；导入模板表头是英文，导出可读性优先）
    ws.append([header for _, header in _EXPORT_COLUMN_ORDER])

    for user in rows:
        role_codes = ",".join(
            r.role_code for r in (user.roles or []) if r.status == "1"
        )
        dept_id_str = ""
        if user.depts:
            dept_id_str = str(user.depts[0].dept_id)

        row_values = []
        for field, _ in _EXPORT_COLUMN_ORDER:
            if field == "role_codes":
                row_values.append(role_codes)
            elif field == "dept_id":
                row_values.append(dept_id_str)
            elif field == "create_time":
                row_values.append(
                    user.create_time.strftime("%Y-%m-%d %H:%M:%S")
                    if user.create_time
                    else ""
                )
            else:
                value = getattr(user, field, "")
                row_values.append("" if value is None else str(value))
        ws.append(row_values)

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


async def export_users_to_excel(
    db: AsyncSession,
    filter_: UserExportFilter,
    current_user: User,
    *,
    reason: str,
    file_storage: FileStorage | None = None,
) -> tuple[bytes, int, str]:
    """导出用户到 Excel（spec §3.6 line 2099-2111 + §2.31 P1-5）。

    流程（v2.2 P1-5 全套）：
    1. reason defense-in-depth 校验（spec §2.30）
    2. 计算 accessible_dept_ids（filter_snapshot 冻结用）
    3. 建 UserExportTask（CREATED）+ flush 拿 export_id
    4. UPDATE task → RUNNING + started_at
    5. 查询用户（filter + data_scope，spec §2.31 line 1545）
    6. 行数 > USER_EXPORT_ASYNC_THRESHOLD → AI_EXPORT_ASYNC_REQUIRED（spec §2.6）
    7. 构造 Excel（EXPORT_ALLOWED_FIELDS 白名单）
    8. 写文件（FileStorage.save，30 天 TTL）
    9. UPDATE task → SUCCESS（row_count / file_storage_key / file_size_bytes /
       finished_at / duration_ms）
    10. 返回 (xlsx_bytes, row_count, export_id)

    失败：UPDATE task → FAILED + error_code + error_message（spec §2.31 line 1567-1572）

    Raises:
        BusinessRuleException:
            - ``AI_EXPORT_REASON_REQUIRED`` — reason 缺失 / 全空白 / >256
            - ``AI_EXPORT_ASYNC_REQUIRED`` — 行数 > 5000（Phase 3 异步通道）
    """
    storage = file_storage or get_file_storage()
    reason_clean = _validate_reason(reason)

    # 1. 计算 accessible_dept_ids（filter_snapshot 冻结用）
    accessible_dept_ids = await _compute_accessible_dept_ids(db, current_user)
    accessible_dept_ids_snapshot: list[int] | None = (
        None if accessible_dept_ids is None else sorted(accessible_dept_ids)
    )

    # 2. 建 task（CREATED）
    task = UserExportTask(
        export_id=str(next_id()),
        operator_id=current_user.user_id,
        filter_snapshot={
            "filter": filter_.model_dump(mode="json"),
            "accessible_dept_ids": accessible_dept_ids_snapshot,
            "filter_evaluated_at": datetime.now().isoformat(),
        },
        reason=reason_clean,
        status=ExportTaskStatus.CREATED,
    )
    db.add(task)
    await db.flush()  # 拿 export_id

    # 3. UPDATE → RUNNING
    started_at = datetime.now()
    task.status = ExportTaskStatus.RUNNING
    task.started_at = started_at
    await db.flush()

    try:
        # 4. 查询用户
        rows = await _query_users_with_data_scope(db, filter_, current_user)

        # 5. 行数限制（spec §2.6）
        if len(rows) > USER_EXPORT_ASYNC_THRESHOLD:
            raise UnprocessableEntityException(
                f"导出行数 {len(rows)} 超过同步阈值 {USER_EXPORT_ASYNC_THRESHOLD}"
                f"，请等待异步通道开放",
                error_code="AI_EXPORT_ASYNC_REQUIRED",
            )

        # 6. 构造 Excel
        xlsx_bytes = _build_excel(rows)

        # 7. 写文件（30 天 TTL）
        storage_key = await storage.save(
            xlsx_bytes,
            mime_type=_EXPORT_MIME_TYPE,
            namespace=_EXPORT_FILE_NAMESPACE,
            suffix=".xlsx",
            ttl_seconds=_EXPORT_FILE_TTL_SECONDS,
        )

        # 8. UPDATE → SUCCESS
        finished_at = datetime.now()
        task.status = ExportTaskStatus.SUCCESS
        task.row_count = len(rows)
        task.file_storage_key = storage_key
        task.file_size_bytes = len(xlsx_bytes)
        task.finished_at = finished_at
        task.duration_ms = int((finished_at - started_at).total_seconds() * 1000)
        await db.flush()

        return xlsx_bytes, len(rows), task.export_id

    except Exception as e:
        # spec §2.31 line 1567-1572：失败也写 task
        finished_at = datetime.now()
        task.status = ExportTaskStatus.FAILED
        task.error_code = getattr(e, "error_code", "") or e.__class__.__name__
        task.error_message = str(e)[:1024]
        task.finished_at = finished_at
        task.duration_ms = int((finished_at - started_at).total_seconds() * 1000)
        await db.flush()
        raise


__all__ = [
    "cleanup_expired_export_tasks",
    "export_users_to_excel",
    "get_export_task",
    "list_export_tasks",
]


async def get_export_task(
    db: AsyncSession,
    export_id: str,
) -> UserExportTask | None:
    """按 export_id 查询导出任务详情（spec §2.31 v2.2 P1-5 line 1589-1591）。

    Args:
        db: 异步数据库会话
        export_id: 任务 ID（Snowflake 字符串）

    Returns:
        UserExportTask | None：找不到返回 None，由 API 层抛
        ``NotFoundException(error_code="AI_EXPORT_TASK_NOT_FOUND")``。
    """
    return await db.get(UserExportTask, export_id)


async def list_export_tasks(
    db: AsyncSession,
    query: UserExportTaskQuery,
) -> PageResult:
    """分页查询导出任务列表（spec §2.31 v2.2 P1-5 line 1593-1595）。

    支持按 operator_id / status 过滤；默认按 created_at desc（最新优先）。
    返回 ``PageResult``，records 是 UserExportTask ORM 实例（API 层负责转
    UserExportTaskResponse）。

    Args:
        db: 异步数据库会话
        query: 分页 + 过滤参数（current/size/operator_id/status）

    Raises:
        BusinessRuleException: ``AI_EXPORT_INVALID_STATUS`` — status 非合法枚举值
    """
    filters: list = []
    if query.operator_id is not None:
        filters.append(UserExportTask.operator_id == query.operator_id)
    if query.status:
        try:
            status_enum = ExportTaskStatus(query.status)
        except ValueError as e:
            raise BusinessRuleException(
                f"非法 status 值：{query.status}",
                error_code="AI_EXPORT_INVALID_STATUS",
            ) from e
        filters.append(UserExportTask.status == status_enum)

    return await paginate(
        db,
        UserExportTask,
        query,
        filters=filters or None,
        order_by=UserExportTask.created_at.desc(),
    )


async def cleanup_expired_export_tasks(db: AsyncSession) -> int:
    """清理 30 天前 ExportTask + 关联 export 文件（spec §2.31 v2.2 P1-5）。

    每日 02:30 cron 入口（``app.tasks.user_cleanup_tasks.clean_expired_export_tasks``）。
    本函数不 commit。

    Returns:
        删除的 task 行数

    设计要点（决策 22.3）：
    - 不分 status 全删（CREATED 30 天前说明异步任务挂了；RUNNING 30 天前是 zombie；
      SUCCESS / FAILED 是终态正常清理）。ExportTask 无状态机 CAS 需求，直接删。
    - file_storage_key 缺失（FAILED task 没生成文件）不抛错
    - file_storage_key 指向不存在的文件（被外部删了）也不抛错（FileStorage.delete
      返 False 而非 raise；MockFileStorage 同款）
    """
    cutoff = datetime.now() - timedelta(days=30)
    fs = get_file_storage()

    stmt = select(UserExportTask).where(UserExportTask.created_at < cutoff)
    tasks = (await db.execute(stmt)).scalars().all()

    for task in tasks:
        if task.file_storage_key:
            try:
                await fs.delete(task.file_storage_key)
            except FileNotFoundError:
                pass
        await db.delete(task)

    if tasks:
        await db.flush()

    return len(tasks)
