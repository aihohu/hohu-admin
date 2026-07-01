from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_permissions
from app.core.base_response import PageResult, ResponseModel
from app.db.session import get_db
from app.modules.system.models.user import User
from app.modules.system.schemas.dict_type import (
    DictTypeCreate,
    DictTypeOut,
    DictTypeQuery,
    DictTypeSimpleOut,
    DictTypeUpdate,
)
from app.modules.system.service.dict_type_service import dict_type_service

router = APIRouter()


@router.get(
    "/list",
    response_model=ResponseModel[PageResult[DictTypeOut]],
    summary="获取字典类型列表分页",
    description="根据查询条件获取字典类型分页列表，支持按字典名称和字典类型筛选",
    responses={
        200: {"description": "获取成功，返回字典类型分页数据"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
    },
    dependencies=[Depends(require_permissions("system:dict-type:list"))],
)
async def get_list(
    query: DictTypeQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """
    获取字典类型分页列表

    Args:
        query: 查询参数，包含分页信息和筛选条件
        db: 异步数据库会话
        _current_user: 当前登录用户对象（用于权限验证）

    Returns:
        ResponseModel: 包含字典类型分页列表的数据

    Query Parameters:
        - current: 当前页码（默认1）
        - size: 每页数量（默认10，最大100）
        - dict_name: 字典名称（支持模糊查询）
        - dict_type: 字典类型（支持模糊查询）
        - status: 字典类型状态（1-启用，2-禁用）
    """
    page_data = await dict_type_service.get_list(db, query)
    return ResponseModel.success(data=page_data)


@router.get(
    "/all",
    response_model=ResponseModel[list[DictTypeSimpleOut]],
    summary="获取全部字典类型列表(不分页)",
    description="获取系统中所有已启用的字典类型列表，不分页，用于下拉选择等场景",
    responses={
        200: {"description": "获取成功，返回字典类型列表"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
    },
)
async def get_all(
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """
    获取所有已启用的字典类型列表

    Args:
        db: 异步数据库会话
        _current_user: 当前登录用户对象（用于权限验证）

    Returns:
        ResponseModel: 包含所有已启用字典类型的列表

    Note:
        - 仅返回状态为启用的字典类型
        - 不分页，返回所有符合条件的字典类型
        - 适合用于前端下拉选择框
    """
    dict_types = await dict_type_service.get_all_enabled(db)
    return ResponseModel.success(data=dict_types)


@router.post(
    "/add",
    summary="创建字典类型",
    description="创建新的字典类型，包括字典名称、编码和描述信息",
    responses={
        200: {"description": "创建成功"},
        400: {"description": "参数验证失败或字典类型已存在"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
    },
    dependencies=[Depends(require_permissions("system:dict-type:add"))],
)
async def add(
    type_in: DictTypeCreate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """
    创建新字典类型

    Args:
        type_in: 字典类型创建信息，包含字典名称、编码、描述和状态
        db: 异步数据库会话
        _current_user: 当前登录用户对象（用于权限验证）

    Returns:
        ResponseModel: 创建成功的消息

    Request Body Fields:
        - dict_name: 字典名称（必填，2-100字符）
        - dict_type: 字典类型（必填，2-100字符）
        - remark: 字典描述（可选，最大500字符）
        - status: 字典类型状态（必填，1-启用，2-禁用）
    """
    await dict_type_service.create(db, type_in)
    await db.commit()
    return ResponseModel.success(msg="字典类型创建成功")


@router.put(
    "/{type_id}",
    summary="编辑字典类型信息",
    description="更新指定字典类型的基本信息",
    responses={
        200: {"description": "更新成功"},
        400: {"description": "参数验证失败"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
        404: {"description": "字典类型不存在"},
    },
    dependencies=[Depends(require_permissions("system:dict-type:edit"))],
)
async def update(
    type_id: int,
    type_in: DictTypeUpdate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """
    更新字典类型信息

    Args:
        type_id: 字典类型ID（路径参数）
        type_in: 字典类型更新信息，所有字段都是可选的
        db: 异步数据库会话
        _current_user: 当前登录用户对象（用于权限验证）

    Returns:
        ResponseModel: 更新成功的消息

    Path Parameters:
        - type_id: 要更新的字典类型ID

    Request Body Fields (Optional):
        - dict_name: 字典名称
        - dict_type: 字典类型
        - remark: 字典描述
        - status: 字典类型状态
    """
    await dict_type_service.update(db, type_id, type_in)
    await db.commit()
    return ResponseModel.success(msg="字典类型更新成功")


@router.delete(
    "/{type_id}",
    summary="删除指定字典类型",
    description="删除指定的单个字典类型",
    responses={
        200: {"description": "删除成功"},
        400: {"description": "字典类型下存在数据"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
        404: {"description": "字典类型不存在"},
    },
    dependencies=[Depends(require_permissions("system:dict-type:delete"))],
)
async def delete(
    type_id: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """
    删除指定字典类型

    Args:
        type_id: 字典类型ID（路径参数）
        db: 异步数据库会话
        _current_user: 当前登录用户对象（用于权限验证）

    Returns:
        ResponseModel: 删除成功的消息

    Path Parameters:
        - type_id: 要删除的字典类型ID

    Note:
        - 此操作不可逆，请谨慎操作
        - 如果字典类型下存在数据，则无法删除
    """
    await dict_type_service.delete(db, type_id)
    await db.commit()
    return ResponseModel.success(msg="字典类型删除成功")


@router.post(
    "/batch-delete",
    summary="批量删除字典类型",
    description="批量删除多个字典类型",
    responses={
        200: {"description": "删除成功"},
        400: {"description": "字典类型下存在数据"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
    },
    dependencies=[Depends(require_permissions("system:dict-type:batch-delete"))],
)
async def batch_delete(
    ids: list[int],
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
):
    """
    批量删除字典类型

    Args:
        request: 批量删除请求，包含字典类型ID列表
        db: 异步数据库会话
        _current_user: 当前登录用户对象（用于权限验证）

    Returns:
        ResponseModel: 删除成功的消息

    Request Body Fields:
        - ids: 字典类型ID列表

    Note:
        - 如果任一字典类型下存在数据，则全部删除失败
    """
    count = await dict_type_service.batch_delete(db, ids)
    await db.commit()
    return ResponseModel.success(msg=f"成功删除 {count} 个字典类型")
