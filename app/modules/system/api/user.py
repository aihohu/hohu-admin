from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.base_response import PageResult, ResponseModel
from app.db.session import get_db
from app.modules.system.models.user import User
from app.modules.system.schemas.user import (
    UserCreate,
    UserItemOut,
    UserQuery,
    UserUpdate,
)
from app.modules.system.service.user_service import user_service

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
    """获取用户分页列表"""
    # 调用 Service 层获取分页数据
    page_data = await user_service.get_user_list(db, query)

    # 转换为 Schema 对象 (处理角色简化)
    user_list = []
    for u in page_data.records:
        item = UserItemOut.model_validate(u)
        item.roles = [r.role_code for r in u.roles]
        user_list.append(item)

    # 返回分页包装结果
    return ResponseModel.success(
        data=PageResult(
            records=user_list,
            total=page_data.total,
            current=page_data.current,
            size=page_data.size,
        )
    )


@router.post("/add", summary="创建用户")
async def add_user(user_in: UserCreate, db: AsyncSession = Depends(get_db)):
    """创建新用户"""
    await user_service.create_user(db, user_in)
    await db.commit()
    return ResponseModel.success(msg="创建成功")


@router.put("/{user_id}", summary="修改用户")
async def update_user(
    user_id: int,
    user_in: UserUpdate,
    db: AsyncSession = Depends(get_db),
):
    """更新用户信息"""
    await user_service.update_user(db, user_id, user_in)
    await db.commit()
    return ResponseModel.success(msg="更新成功")


@router.delete("/{user_id}", summary="删除用户")
async def delete_user(
    user_id: int,
    db: AsyncSession = Depends(get_db),
):
    """删除用户"""
    await user_service.delete_user(db, user_id)
    await db.commit()
    return ResponseModel.success(msg="删除成功")


@router.post("/batch-delete", summary="批量删除用户")
async def batch_delete_users(
    ids: list[int],
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量删除用户"""
    deleted_count = await user_service.batch_delete_users(db, ids, current_user.user_id)
    await db.commit()
    return ResponseModel.success(msg=f"成功删除 {deleted_count} 个用户")
