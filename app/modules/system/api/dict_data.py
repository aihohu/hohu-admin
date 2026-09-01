from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user, require_permissions
from app.core.base_response import PageResult, ResponseModel
from app.core.tenant import TenantContext
from app.db.session import get_db
from app.modules.auth.service import get_current_tenant_context
from app.modules.system.models.user import User
from app.modules.system.schemas.dict_data import (
    DictDataCreate,
    DictDataOut,
    DictDataQuery,
    DictDataUpdate,
)
from app.modules.system.service.dict_data_service import dict_data_service

router = APIRouter()


@router.get(
    "/list",
    response_model=ResponseModel[PageResult[DictDataOut]],
    summary="获取字典数据列表分页",
    description="根据查询条件获取字典数据分页列表，支持按字典标签、字典键值和字典类型筛选",
    responses={
        200: {"description": "获取成功，返回字典数据分页数据"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
    },
    dependencies=[Depends(require_permissions("system:dict-data:list"))],
)
async def get_list(
    query: DictDataQuery = Depends(),
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """
    获取字典数据分页列表

    Args:
        query: 查询参数，包含分页信息和筛选条件
        db: 异步数据库会话
        _current_user: 当前登录用户对象（用于权限验证）

    Returns:
        ResponseModel: 包含字典数据分页列表的数据

    Query Parameters:
        - current: 当前页码（默认1）
        - size: 每页数量（默认10，最大100）
        - dict_label: 字典标签（支持模糊查询）
        - dict_value: 字典键值（支持模糊查询）
        - dict_type: 字典类型（支持模糊查询）
        - status: 字典数据状态（1-启用，2-禁用）
    """
    page_data = await dict_data_service.get_list(db, query, tenant=tenant)
    return ResponseModel.success(data=page_data)


@router.get(
    "/type/{dict_type}",
    response_model=ResponseModel[list[DictDataOut]],
    summary="根据字典类型获取字典数据",
    description="根据字典类型获取所有已启用的字典数据，按排序字段排序",
    responses={
        200: {"description": "获取成功，返回字典数据列表"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
        404: {"description": "字典类型不存在"},
    },
)
async def get_by_type(
    dict_type: str,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """
    根据字典类型获取字典数据

    Args:
        dict_type: 字典类型（路径参数）
        db: 异步数据库会话
        _current_user: 当前登录用户对象（用于权限验证）

    Returns:
        ResponseModel: 包含字典数据列表

    Path Parameters:
        - dict_type: 字典类型编码

    Note:
        - 仅返回状态为启用的字典数据
        - 按 dict_sort 字段排序
    """
    dict_data_list = await dict_data_service.get_by_type(db, dict_type, tenant=tenant)
    return ResponseModel.success(data=dict_data_list)


@router.post(
    "/add",
    summary="创建字典数据",
    description="创建新的字典数据，包括字典标签、键值、排序等信息",
    responses={
        200: {"description": "创建成功"},
        400: {"description": "参数验证失败或字典类型不存在"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
    },
    dependencies=[Depends(require_permissions("system:dict-data:add"))],
)
async def add(
    data_in: DictDataCreate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """
    创建新字典数据

    Args:
        data_in: 字典数据创建信息，包含字典标签、键值、类型、排序等
        db: 异步数据库会话
        _current_user: 当前登录用户对象（用于权限验证）

    Returns:
        ResponseModel: 创建成功的消息

    Request Body Fields:
        - dict_sort: 字典排序（必填，非负整数）
        - dict_label: 字典标签（必填，1-100字符）
        - dict_value: 字典键值（必填，1-100字符）
        - dict_type: 字典类型（必填，1-100字符）
        - css_class: 样式属性（可选，最大100字符）
        - list_class: 表格回显样式（可选，最大100字符）
        - is_default: 是否默认（必填，Y-是，N-否）
        - status: 字典数据状态（必填，1-启用，2-禁用）
    """
    await dict_data_service.create(db, data_in, tenant=tenant)
    await db.commit()
    return ResponseModel.success(msg="字典数据创建成功")


@router.put(
    "/{data_id}",
    summary="编辑字典数据信息",
    description="更新指定字典数据的基本信息",
    responses={
        200: {"description": "更新成功"},
        400: {"description": "参数验证失败或字典类型不存在"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
        404: {"description": "字典数据不存在"},
    },
    dependencies=[Depends(require_permissions("system:dict-data:edit"))],
)
async def update(
    data_id: int,
    data_in: DictDataUpdate,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """
    更新字典数据信息

    Args:
        data_id: 字典数据ID（路径参数）
        data_in: 字典数据更新信息，所有字段都是可选的
        db: 异步数据库会话
        _current_user: 当前登录用户对象（用于权限验证）

    Returns:
        ResponseModel: 更新成功的消息

    Path Parameters:
        - data_id: 要更新的字典数据ID

    Request Body Fields (Optional):
        - dict_sort: 字典排序
        - dict_label: 字典标签
        - dict_value: 字典键值
        - dict_type: 字典类型
        - css_class: 样式属性
        - list_class: 表格回显样式
        - is_default: 是否默认
        - status: 字典数据状态
    """
    await dict_data_service.update(db, data_id, data_in, tenant=tenant)
    await db.commit()
    return ResponseModel.success(msg="字典数据更新成功")


@router.delete(
    "/{data_id}",
    summary="删除指定字典数据",
    description="删除指定的单个字典数据",
    responses={
        200: {"description": "删除成功"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
        404: {"description": "字典数据不存在"},
    },
    dependencies=[Depends(require_permissions("system:dict-data:delete"))],
)
async def delete(
    data_id: int,
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """
    删除指定字典数据

    Args:
        data_id: 字典数据ID（路径参数）
        db: 异步数据库会话
        _current_user: 当前登录用户对象（用于权限验证）

    Returns:
        ResponseModel: 删除成功的消息

    Path Parameters:
        - data_id: 要删除的字典数据ID

    Note:
        - 此操作不可逆，请谨慎操作
    """
    await dict_data_service.delete(db, data_id, tenant=tenant)
    await db.commit()
    return ResponseModel.success(msg="字典数据删除成功")


@router.post(
    "/batch-delete",
    summary="批量删除字典数据",
    description="批量删除多个字典数据，支持传入字典数据ID列表",
    responses={
        200: {"description": "删除成功"},
        401: {"description": "未登录或令牌已过期"},
        403: {"description": "权限不足"},
    },
    dependencies=[Depends(require_permissions("system:dict-data:batch-delete"))],
)
async def batch_delete(
    ids: list[int],
    db: AsyncSession = Depends(get_db),
    _current_user: User = Depends(get_current_user),
    tenant: TenantContext = Depends(get_current_tenant_context),
):
    """
    批量删除字典数据

    Args:
        ids: 字典数据ID列表
        db: 异步数据库会话
        _current_user: 当前登录用户对象（用于权限验证）

    Returns:
        ResponseModel: 删除成功的消息，包含实际删除的数据数量

    Request Body:
        - ids: 字典数据ID数组（如：[123456, 123457, 123458]）

    Note:
        - 此操作不可逆，请谨慎操作
    """
    deleted_count = await dict_data_service.batch_delete(db, ids, tenant=tenant)
    await db.commit()
    return ResponseModel.success(msg=f"成功删除 {deleted_count} 条数据")
