"""用户导出服务。

每次导出都会创建任务记录，并冻结筛选条件、可访问部门和筛选时间，避免后续
组织结构变化导致审计结果漂移。导出原因必填，字段由白名单控制，文件保留
30 天；失败任务也会持久化错误信息。导出没有分块处理，因此不生成批次日志。
超过同步行数上限时要求调用方缩小筛选范围，当前不自动入队。
"""

import io
from datetime import datetime, timedelta

from openpyxl import Workbook
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.base_response import PageResult
from app.core.exceptions import (
    BusinessRuleException,
    NotFoundException,
    UnprocessableEntityException,
)
from app.core.file_storage import FileStorage, get_file_storage
from app.core.id_generator import next_id
from app.core.tenant import PlatformContext, TenantContext
from app.db.base import user_depts
from app.modules.system.constants import (
    EXPORT_ALLOWED_FIELDS,
    USER_EXPORT_ASYNC_THRESHOLD,
    ExportTaskStatus,
)
from app.modules.system.models.dept import Dept
from app.modules.system.models.user import User
from app.modules.system.models.user_transfer import UserExportTask
from app.modules.system.schemas.user_transfer import (
    UserExportFilter,
    UserExportTaskQuery,
)
from app.modules.system.service.user_import_template_service import (
    _build_dept_full_path,
)
from app.modules.system.service.user_import_validator import (
    _compute_accessible_dept_ids,
)
from app.utils.data_scope import get_user_data_scope_filters
from app.utils.pagination import paginate

#: Excel 列顺序，与 ``EXPORT_ALLOWED_FIELDS`` 保持一致。
#: 字段名 ↔ 中文表头，第 1 项是 ORM 属性 / 派生属性名。
#: ``dept_id`` 列展示部门完整路径，而不是数字 ID。
_EXPORT_COLUMN_ORDER: tuple[tuple[str, str], ...] = (
    ("user_name", "账号"),
    ("nickname", "昵称"),
    ("user_email", "邮箱"),
    ("user_phone", "手机号"),
    ("dept_id", "部门"),
    ("role_codes", "角色编码"),
    ("user_gender", "性别"),
    ("status", "状态"),
    ("create_time", "创建时间"),
)
# 防御性断言：列顺序必须与白名单一致，确保未显式允许的字段不会进入 Excel。
assert {col for col, _ in _EXPORT_COLUMN_ORDER} == EXPORT_ALLOWED_FIELDS, (
    "EXPORT_COLUMN_ORDER 必须与 EXPORT_ALLOWED_FIELDS 一致"
)

#: 导出文件保留 30 天。
_EXPORT_FILE_TTL_SECONDS = 30 * 86400

#: 导出文件的存储命名空间。
_EXPORT_FILE_NAMESPACE = "user-export"

#: Excel 文件的 MIME 类型。
_EXPORT_MIME_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: 状态显示文本（数据库取值 1=启用、2=禁用）。
_STATUS_LABELS: dict[str, str] = {"1": "启用", "2": "禁用"}

#: 用户性别显示文本。
_GENDER_LABELS: dict[str, str] = {"0": "未知", "1": "男", "2": "女"}


def _validate_reason(reason: str) -> str:
    """校验导出原因，与导入原因保持相同约束。

    service 层 defense-in-depth：AI tool 直接调 export_service 时也能拦住。
    """
    if reason is None:
        raise BusinessRuleException(
            "reason 必填",
            error_code="AI_EXPORT_REASON_REQUIRED",
        )
    stripped = reason.strip()
    if not stripped or len(stripped) > 256:
        raise BusinessRuleException(
            "reason 必填且长度 1-256 字符",
            error_code="AI_EXPORT_REASON_REQUIRED",
        )
    return stripped


async def _query_users_with_data_scope(
    db: AsyncSession,
    filter_: UserExportFilter,
    current_user: User,
    *,
    tenant: TenantContext,
) -> list[User]:
    """按筛选条件和数据权限查询用户列表。

    - filter 字段：user_name / nickname / user_email / user_phone / status / dept_id
    - data_scope 自动应用：HR 只能导他可见的部门用户
    - 排序：create_time desc（与 list 接口一致）
    """
    stmt = select(User).where(User.tenant_id == tenant.tenant_id)

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
        subq = select(user_depts.c.user_id).where(
            user_depts.c.tenant_id == tenant.tenant_id,
            user_depts.c.dept_id == dept_id_int,
        )
        stmt = stmt.where(User.user_id.in_(subq))

    # data_scope（HR 只能导他可见的部门用户）
    scope_filters = await get_user_data_scope_filters(
        db,
        current_user,
        tenant=tenant,
    )
    for f in scope_filters:
        stmt = stmt.where(f)

    stmt = stmt.order_by(User.create_time.desc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


def _build_excel(rows: list[User], dept_lookup: dict[int, Dept]) -> bytes:
    """按 ``EXPORT_ALLOWED_FIELDS`` 白名单构造 Excel 字节流。

    - 列顺序固定（_EXPORT_COLUMN_ORDER）
    - ``hashed_password`` 永不导出
    - ``role_codes``：逗号分隔启用角色的 ``role_code``
    - ``dept_id``：输出完整路径（总公司/研发中心/前端部），复用
      ``template_service._build_dept_full_path``；多部门场景取第一个 dept
    - ``status``：翻译为启用或禁用
    - ``user_gender``：翻译为未知、男或女
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
        # 部门列输出完整路径，而不是数字 ID。
        dept_display = ""
        if user.depts:
            first_dept = user.depts[0]
            dept_display = _build_dept_full_path(first_dept, dept_lookup)

        row_values = []
        for field, _ in _EXPORT_COLUMN_ORDER:
            if field == "role_codes":
                row_values.append(role_codes)
            elif field == "dept_id":
                row_values.append(dept_display)
            elif field == "status":
                # 翻译为中文标签；未识别值保留原值。
                row_values.append(_STATUS_LABELS.get(user.status, user.status or ""))
            elif field == "user_gender":
                row_values.append(
                    _GENDER_LABELS.get(user.user_gender, user.user_gender or "")
                )
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


async def _build_dept_lookup_for_rows(
    db: AsyncSession, rows: list[User], *, tenant: TenantContext
) -> dict[int, Dept]:
    """预查部门映射，用于生成部门完整路径。

    - 收集所有用户的第一个 dept_id（多部门场景取第一个，与 _build_excel 对齐）
    - 解析每个 dept 的 ancestors → 收集祖先 dept_id
    - 一次性 select 拿到所有 dept（叶子 + 祖先），构建 dept_id → Dept 映射

    避免 N+1：user.depts 已 selectin eager load，但祖先 dept 不在其中，
    需要单独一次性查询。
    """
    if not rows:
        return {}

    # 1. 收集叶子 dept_id
    leaf_ids: set[int] = set()
    for user in rows:
        if user.depts:
            leaf_ids.add(user.depts[0].dept_id)

    if not leaf_ids:
        return {}

    # 2. 查叶子 dept（拿 ancestors）
    leaf_stmt = select(Dept).where(
        Dept.tenant_id == tenant.tenant_id,
        Dept.dept_id.in_(leaf_ids),
    )
    leaf_depts = list((await db.execute(leaf_stmt)).scalars().all())

    # 3. 解析 ancestors 收集祖先 dept_id
    ancestor_ids: set[int] = set()
    for dept in leaf_depts:
        if not dept.ancestors:
            continue
        for raw in dept.ancestors.split(","):
            raw = raw.strip()
            if not raw or raw == "0":
                continue
            try:
                ancestor_ids.add(int(raw))
            except ValueError:
                continue

    # 4. 查祖先 dept（排除已查过的叶子，避免重复）
    missing = ancestor_ids - leaf_ids
    ancestors: list[Dept] = []
    if missing:
        anc_stmt = select(Dept).where(
            Dept.tenant_id == tenant.tenant_id,
            Dept.dept_id.in_(missing),
        )
        ancestors = list((await db.execute(anc_stmt)).scalars().all())

    # 5. 合并构建 dept_lookup
    dept_lookup: dict[int, Dept] = {d.dept_id: d for d in leaf_depts}
    dept_lookup.update({d.dept_id: d for d in ancestors})
    return dept_lookup


async def export_users_to_excel(
    db: AsyncSession,
    filter_: UserExportFilter,
    current_user: User,
    *,
    reason: str,
    file_storage: FileStorage | None = None,
    tenant: TenantContext,
) -> tuple[bytes, int, str]:
    """导出用户到 Excel。

    流程：
    1. 对导出原因执行服务层兜底校验
    2. 计算 accessible_dept_ids（filter_snapshot 冻结用）
    3. 建 UserExportTask（CREATED）+ flush 拿 export_id
    4. UPDATE task → RUNNING + started_at
    5. 按筛选条件和数据权限查询用户
    6. 超过同步行数上限时返回 ``AI_EXPORT_ASYNC_REQUIRED``
    7. 构造 Excel（EXPORT_ALLOWED_FIELDS 白名单）
    8. 写文件（FileStorage.save，30 天 TTL）
    9. UPDATE task → SUCCESS（row_count / file_storage_key / file_size_bytes /
       finished_at / duration_ms）
    10. 返回 (xlsx_bytes, row_count, export_id)

    失败时将任务更新为 ``FAILED``，并记录错误码和错误信息。

    Raises:
        BusinessRuleException:
            - ``AI_EXPORT_REASON_REQUIRED`` — reason 缺失 / 全空白 / >256
            - ``AI_EXPORT_ASYNC_REQUIRED`` — 行数 > 5000，需缩窄筛选后重试
    """
    storage = file_storage or get_file_storage()
    reason_clean = _validate_reason(reason)

    # 1. 计算 accessible_dept_ids（filter_snapshot 冻结用）
    accessible_dept_ids = await _compute_accessible_dept_ids(
        db,
        current_user,
        tenant=tenant,
    )
    accessible_dept_ids_snapshot: list[int] | None = (
        None if accessible_dept_ids is None else sorted(accessible_dept_ids)
    )

    # 2. 建 task（CREATED）
    task = UserExportTask(
        tenant_id=tenant.tenant_id,
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
        rows = await _query_users_with_data_scope(
            db, filter_, current_user, tenant=tenant
        )

        # 5. 同步导出行数限制。
        if len(rows) > USER_EXPORT_ASYNC_THRESHOLD:
            raise UnprocessableEntityException(
                f"导出行数 {len(rows)} 超过同步阈值 {USER_EXPORT_ASYNC_THRESHOLD}"
                "，请缩窄筛选条件后重试",
                error_code="AI_EXPORT_ASYNC_REQUIRED",
            )

        # 6. 预查部门映射并构造 Excel。
        dept_lookup = await _build_dept_lookup_for_rows(db, rows, tenant=tenant)
        xlsx_bytes = _build_excel(rows, dept_lookup)

        # 7. 写文件（30 天 TTL）
        storage_key = await storage.save(
            xlsx_bytes,
            mime_type=_EXPORT_MIME_TYPE,
            namespace=f"tenant-{tenant.tenant_id}-{_EXPORT_FILE_NAMESPACE}",
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
        # 失败任务同样持久化，便于审计和排障。
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
    "download_export_file",
]


async def get_export_task(
    db: AsyncSession,
    export_id: str,
    *,
    operator_id: int,
    allow_cross_owner: bool = False,
    tenant: TenantContext,
) -> UserExportTask | None:
    """按 export_id 查询当前 operator 可见的导出任务。

    Args:
        db: 异步数据库会话
        export_id: 任务 ID（Snowflake 字符串）
        operator_id: 来自认证上下文的当前用户 ID；不得使用请求参数
        allow_cross_owner: 仅 API 显式确认当前用户为 super admin 时传 True

    Returns:
        UserExportTask | None：不存在或不属于当前 operator 均返回 None，由 API 层抛
        ``NotFoundException(error_code="AI_EXPORT_TASK_NOT_FOUND")``。
    """
    stmt = select(UserExportTask).where(
        UserExportTask.tenant_id == tenant.tenant_id,
        UserExportTask.export_id == export_id,
    )
    if not allow_cross_owner:
        stmt = stmt.where(UserExportTask.operator_id == operator_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def list_export_tasks(
    db: AsyncSession,
    query: UserExportTaskQuery,
    *,
    operator_id: int,
    allow_cross_owner: bool = False,
    tenant: TenantContext,
) -> PageResult:
    """分页查询当前 operator 可见的导出任务列表。

    非超管始终追加 ``operator_id == 当前认证用户`` SQL 条件；即使请求中传入
    其他 ``operator_id`` 也不会扩大结果集。仅 ``allow_cross_owner=True`` 时，
    query.operator_id 才能用于选择其他 operator。默认按 created_at desc。
    返回 ``PageResult``，records 是 UserExportTask ORM 实例（API 层负责转
    UserExportTaskResponse）。

    Args:
        db: 异步数据库会话
        query: 分页 + 过滤参数（current/size/operator_id/status）
        operator_id: 来自认证上下文的当前用户 ID；不得使用 query.operator_id
        allow_cross_owner: 仅 API 显式确认当前用户为 super admin 时传 True

    Raises:
        BusinessRuleException: ``AI_EXPORT_INVALID_STATUS`` — status 非合法枚举值
    """
    filters: list = [UserExportTask.tenant_id == tenant.tenant_id]
    if not allow_cross_owner:
        filters.append(UserExportTask.operator_id == operator_id)
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


async def cleanup_expired_export_tasks(
    db: AsyncSession, *, platform: PlatformContext
) -> int:
    """清理 30 天前的导出任务及关联文件。

    每日 02:30 cron 入口（``app.tasks.user_cleanup_tasks.clean_expired_export_tasks``）。
    本函数不 commit。

    Returns:
        删除的 task 行数

    设计要点：
    - 不分 status 全删（CREATED 30 天前说明异步任务挂了；RUNNING 30 天前是 zombie；
      SUCCESS / FAILED 是终态正常清理）。ExportTask 无状态机 CAS 需求，直接删。
    - file_storage_key 缺失（FAILED task 没生成文件）不抛错
    - file_storage_key 指向不存在的文件（被外部删了）也不抛错（FileStorage.delete
      返 False 而非 raise；MockFileStorage 同款）
    """
    if not platform.reason or not platform.correlation_id:
        raise BusinessRuleException("平台清理上下文无效")
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


async def download_export_file(
    db: AsyncSession,
    export_id: str,
    *,
    operator_id: int,
    allow_cross_owner: bool = False,
    file_storage: FileStorage | None = None,
    tenant: TenantContext,
) -> tuple[bytes, str]:
    """按 ``export_id`` 下载已导出的文件。

    AI 对话内闭环关键路径：AI tool 返回 detail_card + downloadUrl，前端
    DetailCardView 渲染下载按钮 → 点击触发本端点 → 返回 xlsx bytes。

    Args:
        db: 异步数据库会话
        export_id: 任务 ID（Snowflake 字符串）
        operator_id: 来自认证上下文的当前用户 ID；不得使用请求参数
        allow_cross_owner: 仅当前用户为 super admin 时显式传 True
        file_storage: 可选注入（测试用 MockFileStorage；生产用 get_file_storage()）

    Returns:
        ``(xlsx_bytes, filename)``；文件名格式为
        ``hohu_users_YYYYMMDD_HHmmss.xlsx``（从 task.created_at 派生，
        与同步导出一致，不重新生成当前时间）。

    Raises:
        NotFoundException: ``AI_EXPORT_TASK_NOT_FOUND`` — export_id 不存在或不属于
            当前 operator（避免泄露任务状态、filter_snapshot 与文件存在性）
        BusinessRuleException: ``AI_EXPORT_TASK_NOT_READY`` — status != SUCCESS
            （FAILED / CREATED / RUNNING zombie）
        BusinessRuleException: ``AI_EXPORT_FILE_MISSING`` — task.file_storage_key
            为 None（FAILED task 或异常路径）
        BusinessRuleException: ``AI_EXPORT_FILE_EXPIRED`` — 文件被 30 天 TTL
            清理 / 外部删除（FileStorage.read 抛 FileNotFoundError）
    """
    task = await get_export_task(
        db,
        export_id,
        operator_id=operator_id,
        allow_cross_owner=allow_cross_owner,
        tenant=tenant,
    )
    if task is None:
        raise NotFoundException(
            "用户导出任务",
            error_code="AI_EXPORT_TASK_NOT_FOUND",
        )

    if task.status != ExportTaskStatus.SUCCESS:
        raise BusinessRuleException(
            f"导出任务未成功（status={task.status.value}），无法下载",
            error_code="AI_EXPORT_TASK_NOT_READY",
        )

    if not task.file_storage_key:
        raise BusinessRuleException(
            "导出文件缺失（task.file_storage_key 为空）",
            error_code="AI_EXPORT_FILE_MISSING",
        )

    storage = file_storage or get_file_storage()
    try:
        xlsx_bytes = await storage.read(task.file_storage_key)
    except FileNotFoundError as e:
        raise BusinessRuleException(
            "导出文件已过期（30 天 TTL 已清理）",
            error_code="AI_EXPORT_FILE_EXPIRED",
        ) from e

    # 从任务创建时间派生稳定文件名，避免下载时间改变文件名。
    filename = f"hohu_users_{task.created_at.strftime('%Y%m%d_%H%M%S')}.xlsx"
    return xlsx_bytes, filename
