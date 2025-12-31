from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import and_, delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.auth import get_current_user
from app.core.base_response import PageResult, ResponseModel
from app.core.security import get_password_hash
from app.db.session import get_db
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.schemas.user import (
    UserCreate,
    UserItemOut,
    UserQuery,
    UserUpdate,
)

router = APIRouter()


@router.get(
    "/list",
    response_model=ResponseModel[PageResult[UserItemOut]],
    summary="获取用户列表分页",
)
async def get_user_list(
    query: UserQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    # 构建查询条件
    filters = []
    if query.user_name:
        filters.append(User.user_name.contains(query.user_name))
    if query.nickname:
        filters.append(User.nickname.contains(query.nickname))
    if query.user_gender:
        filters.append(User.user_gender.contains(query.user_gender))
    if query.user_phone:
        filters.append(User.user_phone.contains(query.user_phone))
    if query.user_email:
        filters.append(User.user_email.contains(query.user_email))
    if query.status:
        filters.append(User.status == query.status)

    # 查询总数
    count_stmt = select(func.count()).select_from(User).where(and_(*filters))
    total = (await db.execute(count_stmt)).scalar() or 0

    # 分页查询数据
    # 使用 selectinload 预加载角色信息
    stmt = (
        select(User)
        .where(and_(*filters))
        .offset((query.current - 1) * query.size)
        .limit(query.size)
        .options(selectinload(User.roles))
        .order_by(User.create_time.desc())
    )
    result = await db.execute(stmt)
    users = result.scalars().all()

    # 转换为 Schema 对象 (处理角色简化)
    user_list = []
    for u in users:
        item = UserItemOut.model_validate(u)
        item.roles = [r.role_name for r in u.roles]
        user_list.append(item)

    # 5. 返回分页包装结果
    page_data = PageResult(
        records=user_list, total=total, current=query.current, size=query.size
    )
    return ResponseModel.success(data=page_data)


@router.post("/add", summary="创建用户")
async def add_user(user_in: UserCreate, db: AsyncSession = Depends(get_db)):
    # 检查唯一性
    result = await db.execute(select(User).where(User.user_name == user_in.user_name))
    if result.scalars().first():
        raise HTTPException(status_code=400, detail="用户名已存在")

    # 准备用户数据
    obj_data = user_in.model_dump(exclude={"roles", "password"})
    new_user = User(**obj_data)
    new_user.hashed_password = get_password_hash(user_in.password)

    # 分配角色
    if user_in.roles:
        role_result = await db.execute(
            select(Role).where(Role.role_code.in_(user_in.roles))
        )
        new_user.roles = role_result.scalars().all()

    db.add(new_user)
    await db.commit()
    return ResponseModel.success(msg="创建成功")


@router.put("/{user_id}", summary="修改用户")
async def update_user(
    user_id: int, user_in: UserUpdate, db: AsyncSession = Depends(get_db)
):
    # 查询用户（带角色预加载）
    stmt = select(User).where(User.user_id == user_id).options(selectinload(User.roles))
    result = await db.execute(stmt)
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")

    # 更新基础字段, 排出roles 和 password
    update_data = user_in.model_dump(exclude={"roles", "password"}, exclude_unset=True)
    for field, value in update_data.items():
        setattr(user, field, value)

    # 更新角色关联
    if user_in.roles is not None:
        role_result = await db.execute(
            select(Role).where(Role.role_code.in_(user_in.roles))
        )
        user.roles = role_result.scalars().all()

    await db.commit()
    return ResponseModel.success(msg="更新成功")


@router.delete("/{user_id}", summary="删除用户")
async def delete_user(user_id: int, db: AsyncSession = Depends(get_db)):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="用户不存在")
    if user.user_name == "admin":
        raise HTTPException(status_code=400, detail="初始管理员不能删除")

    await db.delete(user)
    await db.commit()
    return ResponseModel.success(msg="删除成功")


@router.post("/batch-delete", summary="批量删除用户")
async def batch_delete_users(
    ids: list[int] = Body(...),
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """
    批量删除用户，自动跳过超级管理员
    """
    if not ids:
        return ResponseModel.error(message="未选择要删除的用户")

    # 过滤掉 admin 账号，防止误删
    # 先查询这些 ID 中是否包含 admin
    check_stmt = select(User.user_id).where(
        and_(User.user_id.in_(ids), User.user_name == "admin")
    )
    admin_result = await db.execute(check_stmt)
    if admin_result.scalars().first():
        raise HTTPException(
            status_code=400, detail="所选列表中包含系统管理员，禁止批量删除"
        )

    # 检查是否包含当前用户自己 (防止误删当前登录账号)
    if current_user.user_id in ids:
        raise HTTPException(status_code=400, detail="不能删除当前登录的账号")

    # 执行批量删除
    # 使用 sqlalchemy 的 delete 语句更高效
    stmt = delete(User).where(User.user_id.in_(ids))
    result = await db.execute(stmt)

    # 提交事务
    await db.commit()

    return ResponseModel.success(msg=f"成功删除 {result.rowcount} 个用户")
