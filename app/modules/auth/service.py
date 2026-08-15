import hashlib
import logging
import time

from fastapi import Depends
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.constants import REDIS_BLACKLIST_PREFIX, REDIS_BLACKLIST_TTL
from app.core.base_response import ResponseModel
from app.core.config import settings
from app.core.exceptions import (
    AuthenticationException,
    AuthorizationException,
    BusinessRuleException,
)
from app.core.redis import redis_client
from app.core.security import create_access_token, create_refresh_token, verify_password
from app.db.session import AsyncSessionLocal, get_db
from app.modules.auth.schemas.auth import LoginCredentials, RouteMeta, UserRoute
from app.modules.system.models.login_log import SysLoginLog
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.user import User

logger = logging.getLogger(__name__)


def _token_hash(token: str) -> str:
    """Token 取 SHA256 作为 Redis key，避免原 token 入库。"""
    return hashlib.sha256(token.encode()).hexdigest()


async def _is_blacklisted(token: str) -> bool:
    """检查 token 是否在黑名单（已退出登录）。"""
    result = await redis_client.get(f"{REDIS_BLACKLIST_PREFIX}{_token_hash(token)}")
    return bool(result)


async def _blacklist_token(token: str, expire_at: int | None = None) -> None:
    """把 token 加入黑名单。expire_at 为 token 的 unix 时间戳过期时间。

    用 token 自身的剩余有效期作为 TTL，过期后 Redis 自动清理。
    """
    ttl = (
        max(1, (expire_at or 0) - int(time.time()))
        if expire_at
        else REDIS_BLACKLIST_TTL
    )
    await redis_client.set(
        f"{REDIS_BLACKLIST_PREFIX}{_token_hash(token)}",
        "1",
        ex=ttl,
    )


async def _try_blacklist_token(token: str, expire_at: int | None = None) -> bool:
    """原子「检查并拉黑」：用 SET NX 在单次 Redis 操作里完成。

    返回 True 表示本次调用成功抢到锁（key 之前不存在）；
    返回 False 表示 key 已存在（token 已被 logout 或并发请求先拉黑），
    调用方应据此拒绝请求（防止 refresh token 重放）。
    """
    ttl = (
        max(1, (expire_at or 0) - int(time.time()))
        if expire_at
        else REDIS_BLACKLIST_TTL
    )
    result = await redis_client.set(
        f"{REDIS_BLACKLIST_PREFIX}{_token_hash(token)}",
        "1",
        ex=ttl,
        nx=True,
    )
    return bool(result)


# 定义 OAuth2 方案，指定获取 Token 的 URL
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/auth/token")


class AuthService:
    async def authenticate(
        self,
        credentials: LoginCredentials,
        db: AsyncSession,
        ip: str | None = None,
        user_agent: str | None = None,
    ):
        # 策略分发
        if credentials.login_type == "password":
            user = await self._verify_password_login(credentials, db)
        # elif credentials.login_type == "sms":
        #     user = await self._verify_sms_login(credentials, db)
        # elif credentials.login_type == "google":
        #     user = await self._verify_google_login(credentials, db)
        else:
            raise BusinessRuleException(
                "不支持的登录方式", error_code="UNSUPPORTED_LOGIN_TYPE"
            )

        # 统一签发 Token
        token = create_access_token(subject=str(user.user_id))
        refresh_token = create_refresh_token(subject=str(user.user_id))

        # 写入成功日志
        await self._write_login_log(
            user_id=user.user_id,
            username=user.user_name,
            ip=ip,
            user_agent=user_agent,
            status="1",
            message="登录成功",
        )

        result = {
            "token": token,
            "refreshToken": refresh_token,
        }
        return ResponseModel.success(data=result)

    async def _write_login_log(
        self,
        user_id: int | None,
        username: str,
        ip: str | None,
        user_agent: str | None,
        status: str,
        message: str,
    ):
        """写入登录日志（使用独立 session，不受主事务回滚影响）"""
        try:
            async with AsyncSessionLocal() as session:
                log = SysLoginLog(
                    user_id=user_id,
                    username=username,
                    ip=ip,
                    user_agent=user_agent,
                    status=status,
                    message=message,
                )
                session.add(log)
                await session.commit()
        except Exception:
            logger.exception("Failed to write login log")

    async def _verify_password_login(self, cred, db):
        # 1. 查找用户
        result = await db.execute(select(User).where(User.user_name == cred.user_name))
        user = result.scalars().first()

        # 2. 验证密码
        if not user or not verify_password(cred.password, user.hashed_password):
            raise AuthenticationException(error_code="INVALID_CREDENTIALS")

        if not user.status or user.status == "2":
            raise AuthorizationException("账号已被禁用", error_code="ACCOUNT_DISABLED")

        return user

    async def _verify_sms_login(self, cred, db):
        # 校验 Redis 中的短信码...
        pass


auth_service = AuthService()


async def logout(token: str, refresh_token: str | None = None) -> None:
    """把 access token（和 refresh token，如果提供）加入黑名单，立即失效。

    不需要数据库操作，黑名单走 Redis，TTL 跟 token 剩余有效期对齐，
    过期后 Redis 自动清理（不堆积垃圾）。
    """
    for t in [token, refresh_token]:
        if not t:
            continue
        try:
            payload = jwt.decode(
                t, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
            )
            expire_at = int(payload.get("exp", 0))
        except JWTError:
            continue  # token 已无效，无需加入黑名单
        await _blacklist_token(t, expire_at=expire_at)


async def refresh_access_token(refresh_token: str) -> tuple[str, str]:
    """用 refresh token 换取新的 access + refresh token 对。

    Returns:
        (new_access_token, new_refresh_token)
    Raises:
        AuthenticationException: refresh token 无效/过期/在黑名单中/类型错误/用户已被删除
        AuthorizationException: 用户已被禁用
    """
    try:
        payload = jwt.decode(
            refresh_token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        if payload.get("type") != "refresh":
            raise AuthenticationException("Token 类型错误", error_code="TOKEN_EXPIRED")
        user_id_str = payload.get("sub")
        if not user_id_str:
            raise AuthenticationException("Token 无效", error_code="TOKEN_EXPIRED")
    except JWTError as e:
        raise AuthenticationException(
            "Token 无效或已过期", error_code="TOKEN_EXPIRED"
        ) from e

    # 查 DB 校验用户存在且启用，防止禁用/删除用户用旧 refresh token 持续换新
    user_id = int(user_id_str)
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User).where(User.user_id == user_id))
        user = result.scalars().first()

    if user is None:
        raise AuthenticationException("Token 无效或已过期", error_code="TOKEN_EXPIRED")
    if not user.status or user.status == "2":
        raise AuthorizationException("账号已被禁用", error_code="ACCOUNT_DISABLED")

    # 原子「检查并拉黑」：用 SET NX 保证并发 refresh 同一 token 时只有一个
    # 请求能成功。失败说明 token 已被 logout 拉黑或并发请求先到，按重放拒绝。
    if not await _try_blacklist_token(
        refresh_token, expire_at=int(payload.get("exp", 0))
    ):
        raise AuthenticationException(
            "Token 已失效，请重新登录", error_code="TOKEN_EXPIRED"
        )

    new_access = create_access_token(subject=user_id_str)
    new_refresh = create_refresh_token(subject=user_id_str)
    return new_access, new_refresh


async def get_current_user(
    token: str = Depends(oauth2_scheme), db: AsyncSession = Depends(get_db)
) -> User:
    """
    JWT Token 验证依赖项
    """
    # 0. 黑名单校验（用户已退出登录后 token 立即失效）
    if await _is_blacklisted(token):
        raise AuthenticationException(
            "Token 已失效，请重新登录", error_code="TOKEN_EXPIRED"
        )

    try:
        # 1. 解码 Token
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
        # Only access tokens may authenticate API requests.
        if payload.get("type") != "access":
            raise AuthenticationException("Token 类型错误", error_code="TOKEN_EXPIRED")
        user_id_str: str = payload.get("sub")
        if user_id_str is None:
            raise AuthenticationException(
                "Token 无效或已过期", error_code="TOKEN_EXPIRED"
            )

        # --- 核心修复点：将字符串转为整数 ---
        user_id = int(user_id_str)
    except (JWTError, ValueError):
        raise AuthenticationException("Token 无效或已过期", error_code="TOKEN_EXPIRED")

    # 2. 查询用户并预加载角色和菜单 (RBAC 核心)
    # 使用 selectinload 解决异步环境下的关联查询
    result = await db.execute(
        select(User)
        .where(User.user_id == user_id)
        .options(selectinload(User.roles).selectinload(Role.menus))
    )
    user = result.scalars().first()

    if user is None:
        raise AuthenticationException("Token 无效或已过期", error_code="TOKEN_EXPIRED")

    if not user.status or user.status == "2":
        raise AuthorizationException("账号已被禁用", error_code="ACCOUNT_DISABLED")

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
