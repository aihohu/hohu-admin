from fastapi import APIRouter, Depends, HTTPException
from fastapi.params import Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.static_routes import CONSTANT_ROUTES
from app.core.base_response import ResponseModel
from app.core.security import get_password_hash
from app.db.session import get_db
from app.modules.auth.schemas.auth import LoginCredentials
from app.modules.auth.service import auth_service, build_menu_tree, get_current_user
from app.modules.system.models.menu import Menu
from app.modules.system.models.user import User
from app.modules.system.schemas.user import UserCreate, UserOut

router = APIRouter()


@router.post("/register", response_model=UserOut, summary="用户注册")
async def register(user_in: UserCreate, db: AsyncSession = Depends(get_db)):
    """
    注册新用户：校验重复 -> Hash密码 -> 持久化
    """
    # 检查用户名是否已存在
    result = await db.execute(select(User).where(User.user_name == user_in.user_name))
    if result.scalars().first():
        raise HTTPException(status_code=400, detail="该用户名已被注册")

    # 创建用户实例
    new_user = User(
        user_name=user_in.user_name,
        nickname=user_in.nickname,
        hashed_password=get_password_hash(user_in.password),  # 密码加密
        status="1",
    )

    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)
    return new_user


@router.post("/login", summary="用户登录")
async def login(
    credentials: LoginCredentials,
    db: AsyncSession = Depends(get_db),
):
    result = await auth_service.authenticate(credentials, db)
    return result


@router.get("/getUserInfo", summary="获取当前登录用户信息及权限")
async def get_user_info(current_user: User = Depends(get_current_user)):
    """
    获取用户信息
    """
    # 提取角色编码列表 (如: ['admin', 'user'])
    roles = [role.role_code for role in current_user.roles]

    # 提取按钮级权限标识 (如: ['sys:user:add', 'sys:user:edit'])
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
    "/getUserRoutes", response_model_exclude_none=True, summary="获取动态路由菜单"
)
async def get_user_routes(current_user: User = Depends(get_current_user)):
    """
    获取当前用户的动态路由树
    """
    # 汇总当前用户所有角色下的菜单 (去重)
    all_menus_dict = {}
    for role in current_user.roles:
        for menu in role.menus:
            # 过滤掉按钮级权限，只保留菜单和目录
            if menu.menu_type in ["M", "C"] and menu.status == "1":
                all_menus_dict[menu.menu_id] = menu

    # 构建树形结构
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


@router.get("/isRouteExist", summary="检查路由名称是否存在")
async def is_route_exist(
    route_name: str = Query(..., description="前端路由名称"),
    db: AsyncSession = Depends(get_db),
):
    stmt = select(Menu).where(Menu.route_name == route_name)
    result = await db.execute(stmt)
    exists = result.scalars().first() is not None
    return ResponseModel.success(data=exists)
