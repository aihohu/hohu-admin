from fastapi import APIRouter, Depends
from fastapi.params import Query
from fastapi.security import OAuth2PasswordRequestForm
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import MENU_TYPE_DIRECTORY, MENU_TYPE_MENU, STATUS_ENABLED
from app.constants.static_routes import CONSTANT_ROUTES
from app.core.auth import is_super_admin
from app.core.base_response import ResponseModel
from app.core.exceptions import DuplicateException
from app.core.security import get_password_hash
from app.db.session import get_db
from app.modules.auth.schemas.auth import LoginCredentials
from app.modules.auth.service import auth_service, build_menu_tree, get_current_user
from app.modules.system.models.menu import Menu
from app.modules.system.models.user import User
from app.modules.system.schemas.user import UserCreate, UserOut

router = APIRouter()


@router.post(
    "/register",
    response_model=UserOut,
    summary="用户注册",
    description="注册新用户账号，系统会校验用户名唯一性并加密存储密码",
    responses={
        200: {"description": "注册成功"},
        400: {"description": "用户名已存在或参数验证失败"},
        429: {"description": "请求过于频繁，请稍后再试"},
    },
)
async def register(
    user_in: UserCreate,
    db: AsyncSession = Depends(get_db),
):
    """
    注册新用户

    Args:
        user_in: 用户注册信息，包含用户名、昵称、邮箱、手机号和密码
        db: 异步数据库会话

    Returns:
        UserOut: 注册成功的用户信息（包含生成的 user_id）

    Raises:
        DuplicateException: 当用户名已存在时抛出

    Note:
        - 本接口应用频率限制：每分钟最多 3 次注册尝试
    """
    # 检查用户名是否已存在
    result = await db.execute(select(User).where(User.user_name == user_in.user_name))
    if result.scalars().first():
        raise DuplicateException("用户名", user_in.user_name)

    # 创建用户实例
    new_user = User(
        user_name=user_in.user_name,
        nickname=user_in.nickname,
        hashed_password=get_password_hash(user_in.password),  # 密码加密
        status="1",
    )

    db.add(new_user)
    await db.flush()  # 刷新以获取自增ID
    await db.refresh(new_user)
    return new_user


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
    credentials: LoginCredentials,
    db: AsyncSession = Depends(get_db),
):
    """
    用户登录

    Args:
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
    result = await auth_service.authenticate(credentials, db)
    return result


@router.post(
    "/token",
    summary="Swagger Docs 登录",
    description="OAuth2 兼容的登录端点，供 Swagger UI 的 Authorize 按钮使用",
)
async def login_for_docs(
    form: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """OAuth2 标准登录端点（表单数据），返回 access_token 供 Swagger 使用"""
    credentials = LoginCredentials(
        user_name=form.username,
        password=form.password,
    )
    result = await auth_service.authenticate(credentials, db)
    token = result.data["token"]
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
async def get_user_info(current_user: User = Depends(get_current_user)):
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
    # 提取角色编码列表 (如: ['R_ADMIN', 'R_USER'])
    roles = [role.role_code for role in current_user.roles]

    # 超级管理员直接返回通配符，前端对所有按钮权限放行
    if is_super_admin(current_user):
        return ResponseModel.success(
            data={
                "userId": str(current_user.user_id),
                "userName": current_user.user_name,
                "roles": roles,
                "buttons": ["*"],
            }
        )

    # 提取按钮级权限标识 (如: ['system:user:add', 'system:user:edit'])
    # 遍历用户持有的所有角色，再遍历角色拥有的菜单，提取 permission 字段
    permissions = set()
    for role in current_user.roles:
        for menu in role.menus:
            if menu.permission:  # 只有定义了权限标识的才加入
                permissions.add(menu.permission)

    return ResponseModel.success(
        data={
            "userId": str(current_user.user_id),
            "userName": current_user.user_name,
            # "nickname": current_user.nickname,
            "roles": roles,
            "buttons": list(permissions),
        }
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
    """
    获取当前用户的动态路由树

    Args:
        current_user: 当前登录用户对象（通过 JWT 令牌验证）

    Returns:
        ResponseModel: 包含 home 路由和 routes 路由树的数据

    Note:
        - 仅返回菜单(M)和目录(D)类型的路由
        - 仅返回状态为启用的路由
        - 自动构建树形结构供前端路由使用
        - 超级管理员直接查询所有启用菜单，无需通过角色关联
    """
    if is_super_admin(current_user):
        result = await db.execute(
            select(Menu).where(
                Menu.menu_type.in_([MENU_TYPE_DIRECTORY, MENU_TYPE_MENU]),
                Menu.status == STATUS_ENABLED,
            )
        )
        menu_list = list(result.scalars().all())
    else:
        all_menus_dict = {}
        for role in current_user.roles:
            for menu in role.menus:
                if (
                    menu.menu_type in [MENU_TYPE_DIRECTORY, MENU_TYPE_MENU]
                    and menu.status == STATUS_ENABLED
                ):
                    all_menus_dict[menu.menu_id] = menu
        menu_list = list(all_menus_dict.values())

    route_tree = build_menu_tree(menu_list, 0)

    return ResponseModel.success(
        data={
            "home": "home",
            "routes": route_tree,
        }
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
    route_name: str = Query(..., description="前端路由名称", examples=["system"]),
    db: AsyncSession = Depends(get_db),
):
    """
    检查路由名称是否存在

    Args:
        route_name: 前端路由名称（如：'system', 'user'）
        db: 异步数据库会话

    Returns:
        ResponseModel: 包含布尔值的数据，true 表示路由已存在，false 表示不存在

    Examples:
        >>> is_route_exist(route_name="system")
        {
            "code": 200,
            "msg": "success",
            "data": true
        }
    """
    stmt = select(Menu).where(Menu.route_name == route_name)
    result = await db.execute(stmt)
    exists = result.scalars().first() is not None
    return ResponseModel.success(data=exists)
