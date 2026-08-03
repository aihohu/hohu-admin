"""User import validator（spec §2.17 / §2.18 / §2.15 / §2.11）。

职责：
- resolve_dept：dept_input 名称 / 路径反查 dept_id（#2.17）
- resolve_role_input：role_input 反查 role_codes（#2.18，Task 5 落地）
- check_permission_boundary：批量操作 Permission Boundary 校验（#2.15，Task 6 落地）
- check_dept_data_scope：dept 是否在 operator 的 data_scope 内（#2.11，Task 7 落地）

Service 层调用：dry_run / execute 阶段对每行 record 反查 dept_id / role_codes。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import BusinessRuleException
from app.modules.system.models.dept import Dept


async def resolve_dept(db: AsyncSession, dept_input: str) -> int:
    """反查 dept_input → dept_id（spec §2.17）。

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
