from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.base_response import ResponseModel
from app.core.config import settings
from app.core.security import create_access_token, verify_password
from app.db.session import get_db
from app.modules.auth.schemas.auth import LoginCredentials, RouteMeta, UserRoute
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User

# 定义 OAuth2 方案，指定获取 Token 的 URL
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/login")


class AuthService:
    async def authenticate(self, credentials: LoginCredentials, db: AsyncSession):
        # 策略分发
        if credentials.login_type == "password":
            user = await self._verify_password_login(credentials, db)
        # elif credentials.login_type == "sms":
        #     user = await self._verify_sms_login(credentials, db)
        # elif credentials.login_type == "google":
        #     user = await self._verify_google_login(credentials, db)
        else:
            raise HTTPException(status_code=400, detail="不支持的登录方式")

        # 统一签发 Token
        token = create_access_token(subject=str(user.user_id))
        result = {
            "token": token,
            "refreshToken": "...",  # 如果需要可在此扩展
            # "user": user,
        }
        return ResponseModel.success(data=result)

    async def _verify_password_login(self, cred, db):
        # 1. 查找用户
        result = await db.execute(select(User).where(User.user_name == cred.user_name))
        user = result.scalars().first()

        # 2. 验证密码
        if not user or not verify_password(cred.password, user.hashed_password):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="账号或密码错误"
            )

        if not user.status or user.status == "2":
            raise HTTPException(status_code=403, detail="账号已被禁用")

        return user

    async def _verify_sms_login(self, cred, db):
        # 校验 Redis 中的短信码...
        pass


auth_service = AuthService()


async def get_current_user(
    token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)
) -> User:
    """
    JWT Token 验证依赖项
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token 无效或已过期",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        # 1. 解码 Token
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        user_id_str: str = payload.get("sub")
        if user_id_str is None:
            raise credentials_exception

        # --- 核心修复点：将字符串转为整数 ---
        user_id = int(user_id_str)
    except JWTError:
        raise credentials_exception

    # 2. 查询用户并预加载角色和菜单 (RBAC 核心)
    # 使用 selectinload 解决异步环境下的关联查询
    result = await db.execute(
        select(User)
        .where(User.user_id == user_id)
        .options(selectinload(User.roles).selectinload(Role.menus))
    )
    user = result.scalars().first()

    if user is None:
        raise credentials_exception

    if not user.status or user.status == "2":
        raise HTTPException(status_code=403, detail="账号已被禁用")

    return user


def build_menu_tree(menus: list[Menu], parent_id: int = None) -> list[UserRoute]:
    """
    递归构建路由树
    """
    tree = []
    # 过滤出当前层级的子菜单，并按 order 排序
    current_level_menus = [m for m in menus if m.parent_id == parent_id]
    current_level_menus.sort(key=lambda x: x.order or 0)

    for menu in current_level_menus:
        route = UserRoute(
            name=menu.route_name,
            path=menu.route_path,
            component=menu.component or "basic",
            meta=RouteMeta(
                title=menu.menu_name,
                i18n_key=menu.i18n_key,
                keep_alive=menu.keep_alive,
                constant=menu.constant,
                icon=menu.icon,
                order=menu.order or 0,
                href=menu.href,
                hide_in_menu=menu.hide_in_menu,
                active_menu=menu.active_menu,
                multi_tab=menu.multi_tab,
            ),
        )
        # 递归查找子节点
        children = build_menu_tree(menus, menu.menu_id)
        if children:
            route.children = children

        tree.append(route)
    return tree
