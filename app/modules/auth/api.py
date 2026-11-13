from fastapi import APIRouter, Body, Depends, Request
from fastapi.params import Query
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.static_routes import CONSTANT_ROUTES
from app.core.base_response import ResponseModel
from app.core.config import settings
from app.core.exceptions import (
    AuthenticationException,
)
from app.db.authorization_http import (
    get_authorization_runtime,
    login_http,
    relation_read,
    user_read,
)
from app.db.session import get_db
from app.modules.auth.schemas.auth import LoginCredentials
from app.modules.auth.self_service_api import router as self_service_router
from app.modules.auth.service import (
    get_current_user,
)
from app.modules.auth.service import logout as do_logout
from app.modules.system.models.user import User
from app.utils.ip_util import get_client_ip

router = APIRouter()
router.include_router(self_service_router)


@router.post(
    "/login",
    summary="用户登录",
    description="使用用户名和密码登录系统，验证成功后返回访问令牌",
    responses={
        200: {"description": "登录成功，返回访问令牌"},
        401: {"description": "账号或密码错误"},
        422: {"description": "参数验证失败"},
        429: {"description": "请求过于频繁，请稍后再试"},
    },
)
async def login(
    request: Request,
    credentials: LoginCredentials,
    db: AsyncSession = Depends(get_db),
):
    """
    用户登录

    Args:
        request: HTTP 请求对象（用于获取 IP 和 User-Agent）
        credentials: 登录凭证，包含用户名和密码
        db: 异步数据库会话

    Returns:
        AuthResponse: 登录成功后返回的令牌信息，包含 access_token 和 token_type

    Raises:
        InvalidCredentialsException: 当账号或密码错误时抛出

    Note:
        - 本接口应用频率限制：每分钟最多 5 次登录尝试
        - 连续失败多次后建议等待一段时间再试
    """
    ip = get_client_ip(request)
    user_agent = request.headers.get("User-Agent")
    data = await login_http(
        db,
        credentials,
        ip=ip,
        user_agent=user_agent,
        host=request.headers.get("Host"),
    )
    return ResponseModel.success(data=data)


@router.post(
    "/logout",
    summary="退出登录",
    description="把当前 access/refresh token 加入黑名单，立即失效",
    responses={
        200: {"description": "退出成功"},
        401: {"description": "未登录或令牌已过期"},
    },
)
async def logout(
    request: Request,
    body: dict | None = Body(None, description="可选：{refreshToken: 'xxx'}"),
    current_user: User = Depends(get_current_user),  # noqa: ARG001
):
    """
    退出登录：把 access token（和 refresh token，如果 body 里提供）加入黑名单。
    """
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.removeprefix("Bearer ").strip()
    refresh_token = (body or {}).get("refreshToken")
    if token:
        await do_logout(token, refresh_token=refresh_token)
    return ResponseModel.success(msg="退出成功")


@router.post(
    "/refreshToken",
    summary="刷新 access token",
    description="用 refresh token 换取新的 access + refresh token 对（rotation）",
    responses={
        200: {"description": "刷新成功"},
        401: {"description": "refresh token 无效或已过期"},
    },
)
async def refresh_token(
    body: dict = Body(
        ...,
        openapi_examples={
            "default": {
                "summary": "Refresh access token",
                "value": {"refreshToken": "xxx"},
            }
        },
    ),
):
    """
    Token rotation：旧 refresh token 立即失效，返回全新的 access + refresh 对。
    """
    refresh = body.get("refreshToken", "")
    if not refresh:
        raise AuthenticationException("缺少 refreshToken", error_code="TOKEN_EXPIRED")
    new_access, new_refresh = await get_authorization_runtime().refresh(refresh)
    return ResponseModel.success(
        data={"token": new_access, "refreshToken": new_refresh}
    )


@router.post(
    "/token",
    summary="Swagger Docs 登录",
    description="OAuth2 兼容的登录端点，供 Swagger UI 的 Authorize 按钮使用",
)
async def login_for_docs(
    request: Request,
    form: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """OAuth2 标准登录端点（表单数据），返回 access_token 供 Swagger 使用"""
    credentials = LoginCredentials(
        user_name=form.username,
        password=form.password,
    )
    result = await login_http(
        db,
        credentials,
        host=request.headers.get("Host"),
    )
    token = result["token"]
    return {"access_token": token, "token_type": "bearer"}


@router.get(
    "/getUserInfo",
    summary="获取当前登录用户信息及权限",
    description="获取当前登录用户的详细信息，包括角色列表和按钮级权限标识",
    responses={
        200: {"description": "获取成功"},
        401: {"description": "未登录或令牌已过期"},
    },
)
async def get_user_info(
    current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
):
    """
    获取当前登录用户信息

    Args:
        current_user: 当前登录用户对象（通过 JWT 令牌验证）

    Returns:
        ResponseModel: 包含用户ID、用户名、角色列表和权限按钮列表的数据

    Examples:
        >>> get_user_info(current_user)
        {
            "code": 200,
            "msg": "success",
            "data": {
                "userId": "123456789",
                "userName": "admin",
                "roles": ["R_ADMIN"],
                "buttons": ["sys:user:add", "sys:user:edit"]
            }
        }
    """
    return ResponseModel.success(
        data=await user_read(db, current_user, operation="auth")
    )


@router.get(
    "/getUserRoutes",
    response_model_exclude_none=True,
    summary="获取动态路由菜单",
    description="获取当前用户根据权限动态生成的路由菜单树，仅包含菜单和目录类型的路由",
    responses={
        200: {"description": "获取成功，返回路由树"},
        401: {"description": "未登录或令牌已过期"},
    },
)
async def get_user_routes(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return ResponseModel.success(
        data=await relation_read(db, current_user, operation="runtime.routes")
    )


@router.get(
    "/getConstantRoutes",
    response_model_exclude_none=True,
    summary="获取静态(常量)路由菜单",
    description="获取系统固定的路由配置，这些路由不会随用户权限变化,需要动态请修改接口获取menu中constant=true的数据",
    response_description="静态(常量)路由列表",
)
async def get_constant_routes():
    """
    获取系统静态(常量)路由

    - **返回**: 包含静态(常量)路由的响应模型
    - **注意**: 这些路由是系统固定的，如需动态路由请访问其他接口
    """
    return ResponseModel.success(data=CONSTANT_ROUTES)


@router.get(
    "/isRouteExist",
    summary="检查路由名称是否存在",
    description="检查指定的前端路由名称是否已在系统中存在，用于前端路由冲突检测",
    responses={
        200: {"description": "检查成功，返回布尔值"},
        422: {"description": "参数验证失败"},
    },
)
async def is_route_exist(
    route_name: str = Query(
        ...,
        min_length=1,
        max_length=100,
        description="前端路由名称",
        examples=["system"],
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return ResponseModel.success(
        data=await relation_read(
            db, current_user, operation="runtime.exists", query=route_name
        )
    )


@router.get("/login-options", summary="读取公开登录定位方式")
async def login_options():
    return ResponseModel.success(
        data={
            "defaultLocale": settings.DEFAULT_LOCALE,
            "tenantMode": settings.TENANT_MODE,
            "tenantLocator": "host" if settings.TENANT_HOST_SUFFIX else "code",
        }
    )
