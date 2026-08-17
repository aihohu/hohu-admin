"""用户导入的引用解析、部门范围与同步策略校验。

职责：
- resolve_dept：dept_input 名称 / 路径反查 dept_id（#2.17）
- resolve_role_input：role_input 反查 role_ids（#2.18，code/name 双支持）
- check_dept_data_scope：dept 是否在 operator 的 data_scope 内（#2.11）
- resolve_existing_user + classify_sync_action：employee_no / user_name 命中与同步策略分类

Service 层调用：dry_run / execute 阶段对每行 record 反查 + 越界检查 + 命中分类。
"""

import enum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessRuleException
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.user.constants import EmployeeNoSyncMode
from app.modules.system.user.schemas import FailedRow, UserImportRecord
from app.utils.data_scope import resolve_data_scope


class SyncAction(enum.Enum):
    """应用 sync_mode 后的下一步动作。

    调用方（batch_create_users_from_records）按返回值决定 INSERT/UPDATE/SKIP/FAILED：
    - NEW：existing is None，调用方直接判定（不在 classify_sync_action 范围内）
    - EXISTS_BY_USERNAME：employee_no 兜底按 user_name 命中 → 调用方按 on_conflict 处理
    - REJECT_EMPLOYEE_NO_EXISTS：CREATE_ONLY + employee_no 命中 → FailedRow
    - UPDATE_SAFE：UPDATE_PROFILE + employee_no 命中 → 仅更新 OVERWRITE_ALLOWED（不动 user_name）
    - UPDATE_FULL：FULL_SYNC + employee_no 命中 → 含 user_name 全字段更新
    """

    NEW = "NEW"
    EXISTS_BY_USERNAME = "EXISTS_BY_USERNAME"
    REJECT_EMPLOYEE_NO_EXISTS = "REJECT_EMPLOYEE_NO_EXISTS"
    UPDATE_SAFE = "UPDATE_SAFE"
    UPDATE_FULL = "UPDATE_FULL"


async def resolve_dept(db: AsyncSession, dept_input: str) -> int:
    """将 dept_input 解析为唯一 dept_id。

    dept_input 不含 ``/`` → 名称模式：唯一性校验，重名抛 DUPLICATE。
    dept_input 含 ``/`` → 路径模式：按 ``/`` 拆段逐级走 parent_id 链。

    禁用部门（status='2'）一律视为不存在（防用户被分配到停用部门）。

    Raises:
        BusinessRuleException:
            - ``AI_IMPORT_DEPT_NOT_FOUND`` — 名称模式无启用匹配
            - ``AI_IMPORT_DEPT_DUPLICATE`` — 名称模式多匹配，引导改用路径
            - ``AI_IMPORT_DEPT_PATH_NOT_FOUND`` — 路径某段无启用匹配
    """
    dept_input = dept_input.strip()
    if not dept_input:
        raise BusinessRuleException(
            "dept_input 不能为空",
            error_code="AI_IMPORT_DEPT_NOT_FOUND",
        )

    if "/" in dept_input:
        return await _resolve_dept_by_path(db, dept_input)
    return await _resolve_dept_by_name(db, dept_input)


async def _resolve_dept_by_name(db: AsyncSession, name: str) -> int:
    """名称模式：dept_name == name + status='1'，唯一性校验。"""
    depts = (
        (
            await db.execute(
                select(Dept.dept_id).where(
                    Dept.dept_name == name,
                    Dept.status == "1",  # noqa: E712
                )
            )
        )
        .scalars()
        .all()
    )

    if not depts:
        raise BusinessRuleException(
            f"部门不存在: {name}",
            error_code="AI_IMPORT_DEPT_NOT_FOUND",
        )
    if len(depts) > 1:
        raise BusinessRuleException(
            f"找到 {len(depts)} 个同名部门 '{name}'，请用完整路径 '父部门/子部门'",
            error_code="AI_IMPORT_DEPT_DUPLICATE",
        )
    return depts[0]


async def _resolve_dept_by_path(db: AsyncSession, path: str) -> int:
    """路径模式：按 / 拆段，逐级走 parent_id 链。"""
    parts = [p.strip() for p in path.split("/") if p.strip()]
    if not parts:
        raise BusinessRuleException(
            f"部门路径为空: {path}",
            error_code="AI_IMPORT_DEPT_PATH_NOT_FOUND",
        )

    parent_id: int | None = None
    current_id: int | None = None
    for name in parts:
        stmt = select(Dept.dept_id).where(
            Dept.dept_name == name,
            Dept.status == "1",  # noqa: E712
        )
        if parent_id is None:
            stmt = stmt.where(Dept.parent_id.is_(None))
        else:
            stmt = stmt.where(Dept.parent_id == parent_id)
        result = (await db.execute(stmt)).scalar_one_or_none()
        if result is None:
            raise BusinessRuleException(
                f"部门路径不存在: {path}（在 '{name}' 段断裂）",
                error_code="AI_IMPORT_DEPT_PATH_NOT_FOUND",
            )
        parent_id = result
        current_id = result

    # current_id 必非空：parts 非空 + 循环必赋值，但 type-checker 看不出
    assert current_id is not None
    return current_id


async def resolve_role_input(db: AsyncSession, role_input_str: str) -> list[int]:
    """将 role_input 解析为 role_id 列表。

    支持逗号分隔的 code/name 混合输入（如 ``"R_DEV,开发者,R_QA"``）。
    两轮匹配：Pass 1 role_code 精确匹配，Pass 2 role_name 精确匹配剩余项。
    最后 ``set()`` 去重（防 'R_DEV,开发者' 同一角色写两次）。

    禁用角色（status='2'）一律视为不存在（与 resolve_dept 一致）。
    role_code / role_name 都 UNIQUE，所以两者都能精确匹配（无一对多）。

    Raises:
        BusinessRuleException: ``AI_IMPORT_ROLE_NOT_FOUND`` — 任一项 code/name
            都未匹配，错误信息含 remaining 列表（已匹配项不进异常信息）。
    """
    parts = [p.strip() for p in role_input_str.split(",") if p.strip()]
    if not parts:
        return []

    # Pass 1: role_code 精确匹配（status='1' 过滤禁用）
    by_code = (
        await db.execute(
            select(Role.role_id, Role.role_code).where(
                Role.role_code.in_(parts),
                Role.status == "1",  # noqa: E712
            )
        )
    ).all()
    matched_codes = {row.role_code for row in by_code}
    role_ids = [row.role_id for row in by_code]

    # Pass 2: 剩余项按 role_name 精确匹配
    remaining = [p for p in parts if p not in matched_codes]
    if remaining:
        by_name = (
            await db.execute(
                select(Role.role_id, Role.role_name).where(
                    Role.role_name.in_(remaining),
                    Role.status == "1",  # noqa: E712
                )
            )
        ).all()
        matched_names = {row.role_name for row in by_name}
        role_ids.extend(row.role_id for row in by_name)
        remaining = [p for p in remaining if p not in matched_names]

    if remaining:
        raise BusinessRuleException(
            f"角色不存在（code/name 都未匹配）: {','.join(remaining)}",
            error_code="AI_IMPORT_ROLE_NOT_FOUND",
        )

    return list(set(role_ids))


async def _compute_accessible_dept_ids(db: AsyncSession, user: User) -> set[int] | None:
    """Return the shared union resolver's accessible department IDs.

    Returns:
        ``None`` means unbounded. An empty set means the principal cannot
        assign an imported user to any department.
    """
    resolution = await resolve_data_scope(db, user)
    if resolution.unbounded:
        return None
    return set(resolution.accessible_dept_ids or ())


async def check_dept_data_scope(
    db: AsyncSession,
    records: list[UserImportRecord],
    current_user: User,
) -> list[FailedRow]:
    """对每行执行部门数据权限校验。

    防 HR 把用户塞到超管部门绕过权限（权限提升攻击主入口之一）。

    流程：
    1. 计算 accessible_dept_ids（None=全部可见 → 跳过）
    2. 每行 resolve_dept → requested_dept_id
       - resolve 失败 → FailedRow(对应 error_code)
       - 不在 accessible_dept_ids → FailedRow(AI_IMPORT_DEPT_OUT_OF_SCOPE)
    """
    accessible_dept_ids = await _compute_accessible_dept_ids(db, current_user)
    if accessible_dept_ids is None:
        return []

    failed_rows: list[FailedRow] = []
    for record in records:
        try:
            requested_dept_id = await resolve_dept(db, record.dept_input)
        except BusinessRuleException as exc:
            failed_rows.append(
                FailedRow(
                    row_num=record.row_num,
                    field="dept_input",
                    value=record.dept_input,
                    reason=str(exc),
                    error_code=exc.error_code,
                )
            )
            continue

        if requested_dept_id not in accessible_dept_ids:
            failed_rows.append(
                FailedRow(
                    row_num=record.row_num,
                    field="dept_input",
                    value=record.dept_input,
                    reason="部门不在当前用户的数据权限范围内",
                    error_code="AI_IMPORT_DEPT_OUT_OF_SCOPE",
                )
            )

    return failed_rows


async def resolve_existing_user(
    db: AsyncSession, record: UserImportRecord
) -> tuple[User | None, bool]:
    """按 employee_no 优先、user_name 兜底反查已有用户。

    匹配顺序：
    1. record.employee_no 非空 → select User where employee_no == record.employee_no
    2. 退化到 user_name 匹配

    Returns:
        (user, matched_by_employee_no):
        - (None, False) — 无任何匹配（新建场景）
        - (user, True) — 按 employee_no 命中（sync_mode 决定后续行为）
        - (user, False) — 按 user_name 命中，后续按 on_conflict 处理
    """
    if record.employee_no:
        existing = (
            await db.execute(select(User).where(User.employee_no == record.employee_no))
        ).scalar_one_or_none()
        if existing is not None:
            return existing, True

    existing = (
        await db.execute(select(User).where(User.user_name == record.user_name))
    ).scalar_one_or_none()
    return existing, False


def classify_sync_action(
    matched_by_employee_no: bool,
    sync_mode: EmployeeNoSyncMode,
) -> SyncAction:
    """根据命中方式和 sync_mode 决定后续动作。

    employee_no 命中 → sync_mode 决定 REJECT / UPDATE_SAFE / UPDATE_FULL
    user_name 命中时一律返回 EXISTS_BY_USERNAME，由调用方按 on_conflict 处理。

    Args:
        matched_by_employee_no: resolve_existing_user 返回的 matched 标志
        sync_mode: 调用方传入的策略

    Returns:
        SyncAction 枚举值（NEW 由调用方在 user=None 时直接判定）
    """
    if not matched_by_employee_no:
        return SyncAction.EXISTS_BY_USERNAME

    if sync_mode == EmployeeNoSyncMode.CREATE_ONLY:
        return SyncAction.REJECT_EMPLOYEE_NO_EXISTS
    if sync_mode == EmployeeNoSyncMode.UPDATE_PROFILE:
        return SyncAction.UPDATE_SAFE
    return SyncAction.UPDATE_FULL
