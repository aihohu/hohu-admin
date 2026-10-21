from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.constants import ADMIN_USERNAME, REDIS_USER_NAME_PREFIX
from app.core.config import settings
from app.core.exceptions import (
    BusinessRuleException,
    DuplicateException,
    InvalidParameterException,
    NotFoundException,
)
from app.core.redis import redis_client
from app.core.security import get_password_hash, verify_password
from app.core.tenant import TenantContext
from app.core.tenant_scope import tenant_cache_key, tenant_filter, tenant_select
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.schemas.user import (
    ChangePassword,
    ProfileOut,
    ResetPassword,
    UpdateProfile,
    UserCreate,
    UserQuery,
    UserUpdate,
)
from app.modules.system.service.settings_service import settings_service
from app.utils.data_scope import get_user_data_scope_filters
from app.utils.pagination import build_filters, paginate


class UserService:
    """用户业务逻辑服务"""

    async def get_user_list(
        self,
        db: AsyncSession,
        query: UserQuery,
        current_user: User | None = None,
        *,
        tenant: TenantContext,
    ):
        """
        获取用户分页列表（含数据权限过滤）

        Args:
            db: 数据库会话
            query: 查询参数
            current_user: 当前登录用户（用于数据权限过滤）

        Returns:
            分页数据对象
        """
        field_mapping = {
            "user_name": ("user_name", "contains"),
            "nickname": ("nickname", "contains"),
            "user_gender": ("user_gender", "contains"),
            "user_phone": ("user_phone", "contains"),
            "user_email": ("user_email", "contains"),
            "status": ("status", "=="),
            "role_code": lambda model, val: model.roles.any(Role.role_code == val),
        }
        filters = build_filters(User, field_mapping, **query.model_dump())
        filters.insert(0, tenant_filter(User, tenant=tenant))

        # 应用数据权限过滤
        if current_user is not None:
            scope_filters = await get_user_data_scope_filters(
                db,
                current_user,
                tenant=tenant,
            )
            filters.extend(scope_filters)

        page_data = await paginate(
            db=db,
            model=User,
            query_params=query,
            filters=filters,
            order_by=User.create_time.desc(),
            eager_loads=[selectinload(User.roles), selectinload(User.depts)],
        )

        return page_data

    async def user_exists(
        self, db: AsyncSession, user_id: int, *, tenant: TenantContext
    ) -> bool:
        """Return whether a stable user identifier still exists."""
        return (
            await db.scalar(
                select(User.user_id)
                .where(
                    User.tenant_id == tenant.tenant_id,
                    User.user_id == user_id,
                )
                .limit(1)
            )
        ) is not None

    async def get_user_names_by_ids(
        self,
        db: AsyncSession,
        user_ids: set[int],
        *,
        tenant: TenantContext,
    ) -> dict[int, str]:
        """Resolve non-sensitive user names for trusted cross-module audit data."""
        if not user_ids:
            return {}
        rows = (
            await db.execute(
                select(User.user_id, User.user_name)
                .where(
                    User.tenant_id == tenant.tenant_id,
                    User.user_id.in_(user_ids),
                )
                .order_by(User.user_id)
            )
        ).all()
        return {int(user_id): user_name for user_id, user_name in rows}

    async def create_user(
        self, db: AsyncSession, user_in: UserCreate, *, tenant: TenantContext
    ) -> User:
        """
        创建新用户

        Args:
            db: 数据库会话
            user_in: 用户创建数据

        Returns:
            创建的用户对象

        Raises:
            DuplicateException: 用户名已存在
        """
        # 检查唯一性
        result = await db.execute(
            tenant_select(User, tenant=tenant).where(
                User.user_name == user_in.user_name
            )
        )
        if result.scalars().first():
            raise DuplicateException("用户名", user_in.user_name)

        # 准备用户数据
        obj_data = user_in.model_dump(exclude={"role_ids", "password", "dept_ids"})
        new_user = User(tenant_id=tenant.tenant_id, **obj_data)
        new_user.hashed_password = get_password_hash(user_in.password)

        db.add(new_user)
        return new_user

    async def update_user(
        self,
        db: AsyncSession,
        user_id: int,
        user_in: UserUpdate,
        *,
        tenant: TenantContext,
    ) -> User:
        """
        更新用户信息

        Args:
            db: 数据库会话
            user_id: 用户ID
            user_in: 用户更新数据

        Returns:
            更新后的用户对象

        Raises:
            NotFoundException: 用户不存在
        """
        # Serialize profile and status changes with authorization writers.
        stmt = (
            tenant_select(User, tenant=tenant)
            .where(User.user_id == user_id)
            .options(selectinload(User.roles))
            .with_for_update()
        )
        result = await db.execute(stmt)
        user = result.scalars().first()
        if not user:
            raise NotFoundException("用户")

        # Update profile fields only; roles and departments use dedicated writers.
        update_data = user_in.model_dump(exclude={"password"}, exclude_unset=True)
        status_changed = (
            "status" in update_data and update_data["status"] != user.status
        )
        username_changed = (
            "user_name" in update_data and update_data["user_name"] != user.user_name
        )
        if "user_email" in update_data and update_data["user_email"] != user.user_email:
            user.email_verified_at = None
        for field, value in update_data.items():
            setattr(user, field, value)
        if status_changed:
            user.auth_version += 1

        # 改名后失效审计中间件的 username 缓存，避免 5 分钟内日志记旧名
        if username_changed:
            await redis_client.delete(
                tenant_cache_key(tenant, REDIS_USER_NAME_PREFIX.rstrip(":"), user_id)
            )

        return user

    async def reset_password(
        self,
        db: AsyncSession,
        user_id: int,
        reset_in: ResetPassword,
        *,
        tenant: TenantContext,
    ) -> None:
        """
        管理员重置用户密码

        Args:
            db: 数据库会话
            user_id: 用户ID
            reset_in: 重置密码数据

        Raises:
            NotFoundException: 用户不存在
        """
        user = await db.scalar(
            tenant_select(User, tenant=tenant)
            .where(User.user_id == user_id)
            .with_for_update()
        )
        if not user:
            raise NotFoundException("用户")
        user.hashed_password = get_password_hash(reset_in.new_password)
        user.auth_version += 1

    async def revoke_sessions(
        self,
        db: AsyncSession,
        user_id: int,
        *,
        tenant: TenantContext,
    ) -> None:
        """Atomically invalidate every tenant token previously issued to a user."""
        user = await db.scalar(
            tenant_select(User, tenant=tenant)
            .where(User.user_id == user_id)
            .with_for_update()
        )
        if not user:
            raise NotFoundException("用户")
        user.auth_version += 1

    async def delete_user(
        self, db: AsyncSession, user_id: int, *, tenant: TenantContext
    ) -> None:
        """
        删除用户

        Args:
            db: 数据库会话
            user_id: 用户ID

        Raises:
            NotFoundException: 用户不存在
            BusinessRuleException: 不能删除系统管理员
        """
        users = await self.prepare_delete_dependencies(db, [user_id], tenant=tenant)
        await db.delete(users[0])

    async def batch_delete_users(
        self,
        db: AsyncSession,
        ids: list[int],
        current_user_id: int,
        *,
        tenant: TenantContext,
    ) -> int:
        """
        批量删除用户

        Args:
            db: 数据库会话
            ids: 用户ID列表
            current_user_id: 当前登录用户ID

        Returns:
            删除的用户数量

        Raises:
            InvalidParameterException: 未选择要删除的用户
            BusinessRuleException: 不能删除系统管理员
            BusinessRuleException: 不能删除当前登录的账号
        """
        if not ids:
            raise InvalidParameterException("未选择要删除的用户")

        # 检查是否包含当前用户自己 (防止误删当前登录账号)
        if current_user_id in ids:
            raise BusinessRuleException("不能删除当前登录的账号")

        users = await self.prepare_delete_dependencies(db, ids, tenant=tenant)
        matched_ids = {user.user_id for user in users}
        stmt = delete(User).where(
            User.tenant_id == tenant.tenant_id,
            User.user_id.in_(matched_ids),
        )
        result = await db.execute(stmt)

        return result.rowcount

    async def prepare_delete_dependencies(
        self, db: AsyncSession, ids: list[int], *, tenant: TenantContext
    ) -> list[User]:
        """Lock/recheck deletion dependencies before invoking an AI handler.

        A Gateway preflight may retry a changed plan in a fresh transaction;
        once this returns, the handler reuses these locks without retrying.
        """
        from app.modules.ai.service.user_delete_dependency_service import (  # noqa: PLC0415
            user_delete_dependency_service,
        )

        target_ids = set(ids)
        stmt = tenant_select(User, tenant=tenant).where(
            User.user_id.in_(sorted(target_ids))
        )
        # Preserve the existing tenant-scoped not-found/admin boundary before
        # discovering any AI facts. This read grants no deletion authority.
        users = list((await db.scalars(stmt)).all())
        if {user.user_id for user in users} != target_ids:
            raise NotFoundException("用户")
        if any(user.user_name == ADMIN_USERNAME for user in users):
            raise BusinessRuleException("不能删除系统管理员")
        plan = user_delete_dependency_service.locked_plan(
            db, target_user_ids=ids, tenant=tenant
        )
        if plan is None:
            plan = await user_delete_dependency_service.plan(
                db, target_user_ids=ids, tenant=tenant
            )
            await user_delete_dependency_service.lock_plan(db, plan, tenant=tenant)
        # FOR UPDATE (not NO KEY UPDATE) also fences new FK conversations.
        users = list(
            (
                await db.scalars(
                    stmt.order_by(User.user_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
            ).all()
        )
        if {user.user_id for user in users} != target_ids:
            raise NotFoundException("用户")
        if any(user.user_name == ADMIN_USERNAME for user in users):
            raise BusinessRuleException("不能删除系统管理员")
        await user_delete_dependency_service.recheck_after_user_locks(
            db, plan, tenant=tenant
        )
        return users

    @staticmethod
    def build_batch_delete_identity_snapshot(users: list[User]) -> dict:
        """Build the stable identity snapshot bound to a destructive approval."""
        ordered_users = sorted(users, key=lambda user: user.user_id)
        return {
            "targets": [
                {
                    "userId": str(user.user_id),
                    "userName": user.user_name,
                    "userPhone": user.user_phone,
                }
                for user in ordered_users
            ]
        }

    async def get_batch_delete_identity_snapshot(
        self,
        db: AsyncSession,
        user_ids: list[int],
        *,
        tenant: TenantContext,
    ) -> dict:
        result = await db.execute(
            tenant_select(User, tenant=tenant)
            .where(User.user_id.in_(user_ids))
            .order_by(User.user_id.asc())
        )
        return self.build_batch_delete_identity_snapshot(list(result.scalars().all()))

    def get_profile(self, current_user: User) -> ProfileOut:
        """获取当前用户个人信息"""
        profile = ProfileOut(
            user_id=current_user.user_id,
            user_name=current_user.user_name,
            nickname=current_user.nickname or "",
            user_gender=current_user.user_gender or "0",
            user_phone=current_user.user_phone or "",
            user_email=current_user.user_email or "",
            user_avatar=current_user.user_avatar or "",
            status=current_user.status,
            roles=[r.role_name for r in current_user.roles],
            create_time=(
                current_user.create_time.strftime("%Y-%m-%d %H:%M:%S")
                if current_user.create_time
                else ""
            ),
        )
        return profile

    def update_profile(self, current_user: User, body: UpdateProfile) -> None:
        """更新当前用户个人信息"""
        update_data = body.model_dump(exclude_unset=True)
        if (
            "user_email" in update_data
            and update_data["user_email"] != current_user.user_email
        ):
            current_user.email_verified_at = None
        for field, value in update_data.items():
            setattr(current_user, field, value)

    async def change_password(
        self,
        db: AsyncSession,
        user_id: int,
        body: ChangePassword,
        *,
        tenant: TenantContext,
    ) -> None:
        """Serialize password verification and revoke all older user sessions."""
        user = await db.scalar(
            tenant_select(User, tenant=tenant)
            .where(User.user_id == user_id)
            .with_for_update()
        )
        if not user:
            raise NotFoundException("用户")
        if not verify_password(body.old_password, user.hashed_password):
            raise BusinessRuleException(
                "当前密码不正确", error_code="INCORRECT_OLD_PASSWORD"
            )
        user.hashed_password = get_password_hash(body.new_password)
        user.auth_version += 1


DEFAULT_PASSWORD_CONFIG_KEY = "auth:default_password"
"""Configuration key used to resolve the initial password for new users."""

INSECURE_DEFAULT_PASSWORD_SENTINELS = frozenset({"Hohu123456"})
"""Public development seeds that must not be used in production."""


async def get_default_password(db: AsyncSession, *, tenant: TenantContext) -> str:
    """Return the active initial password configured for user creation."""
    value = await settings_service.get_value(
        db, DEFAULT_PASSWORD_CONFIG_KEY, tenant=tenant
    )
    if value is None or not value.strip():
        raise BusinessRuleException(
            "默认密码未配置（sys_setting.auth:default_password），无法导入新用户",
            error_code="AI_IMPORT_DEFAULT_PASSWORD_NOT_SET",
        )
    if settings.ENV == "prod" and value in INSECURE_DEFAULT_PASSWORD_SENTINELS:
        raise BusinessRuleException(
            "生产环境禁止使用公开的初始化默认密码，请先更新 auth:default_password",
            error_code="AI_IMPORT_DEFAULT_PASSWORD_INVALID",
        )
    return value


# Module-level singleton shared by request handlers and internal adapters.
user_service = UserService()
