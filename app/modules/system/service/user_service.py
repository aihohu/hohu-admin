from sqlalchemy import and_, delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.constants import ADMIN_USERNAME
from app.core.exceptions import (
    CannotDeleteAdminException,
    CannotDeleteSelfException,
    DuplicateUserException,
    InvalidParameterException,
    UserNotFoundException,
)
from app.core.security import get_password_hash
from app.modules.system.models.role import Role
from app.modules.system.models.user import User
from app.modules.system.schemas.user import UserCreate, UserQuery, UserUpdate
from app.utils.pagination import build_filters, paginate


class UserService:
    """用户业务逻辑服务"""

    async def get_user_list(self, db: AsyncSession, query: UserQuery):
        """
        获取用户分页列表

        Args:
            db: 数据库会话
            query: 查询参数

        Returns:
            分页数据对象
        """
        # 构建查询条件
        field_mapping = {
            "user_name": ("user_name", "contains"),
            "nickname": ("nickname", "contains"),
            "user_gender": ("user_gender", "contains"),
            "user_phone": ("user_phone", "contains"),
            "user_email": ("user_email", "contains"),
            "status": ("status", "=="),
        }
        filters = build_filters(User, field_mapping, **query.model_dump())

        # 使用通用分页查询
        page_data = await paginate(
            db=db,
            model=User,
            query_params=query,
            filters=filters,
            order_by=User.create_time.desc(),
            eager_loads=[selectinload(User.roles)],
        )

        return page_data

    async def create_user(self, db: AsyncSession, user_in: UserCreate) -> User:
        """
        创建新用户

        Args:
            db: 数据库会话
            user_in: 用户创建数据

        Returns:
            创建的用户对象

        Raises:
            DuplicateUserException: 用户名已存在
        """
        # 检查唯一性
        result = await db.execute(
            select(User).where(User.user_name == user_in.user_name)
        )
        if result.scalars().first():
            raise DuplicateUserException(user_in.user_name)

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
        return new_user

    async def update_user(
        self, db: AsyncSession, user_id: int, user_in: UserUpdate
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
            UserNotFoundException: 用户不存在
        """
        # 查询用户（带角色预加载）
        stmt = (
            select(User)
            .where(User.user_id == user_id)
            .options(selectinload(User.roles))
        )
        result = await db.execute(stmt)
        user = result.scalars().first()
        if not user:
            raise UserNotFoundException()

        # 更新基础字段，排除 roles 和 password
        update_data = user_in.model_dump(
            exclude={"roles", "password"}, exclude_unset=True
        )
        for field, value in update_data.items():
            setattr(user, field, value)

        # 更新角色关联
        if user_in.roles is not None:
            role_result = await db.execute(
                select(Role).where(Role.role_code.in_(user_in.roles))
            )
            user.roles = role_result.scalars().all()

        return user

    async def delete_user(self, db: AsyncSession, user_id: int) -> None:
        """
        删除用户

        Args:
            db: 数据库会话
            user_id: 用户ID

        Raises:
            UserNotFoundException: 用户不存在
            CannotDeleteAdminException: 尝试删除管理员账号
        """
        user = await db.get(User, user_id)
        if not user:
            raise UserNotFoundException()
        if user.user_name == ADMIN_USERNAME:
            raise CannotDeleteAdminException()

        await db.delete(user)

    async def batch_delete_users(
        self, db: AsyncSession, ids: list[int], current_user_id: int
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
            CannotDeleteAdminException: 尝试删除管理员账号
            CannotDeleteSelfException: 尝试删除当前登录账号
        """
        if not ids:
            raise InvalidParameterException("未选择要删除的用户")

        # 过滤掉 admin 账号，防止误删
        check_stmt = select(User.user_id).where(
            and_(User.user_id.in_(ids), User.user_name == ADMIN_USERNAME)
        )
        admin_result = await db.execute(check_stmt)
        if admin_result.scalars().first():
            raise CannotDeleteAdminException("系统管理员")

        # 检查是否包含当前用户自己 (防止误删当前登录账号)
        if current_user_id in ids:
            raise CannotDeleteSelfException()

        # 执行批量删除
        stmt = delete(User).where(User.user_id.in_(ids))
        result = await db.execute(stmt)

        return result.rowcount


# 创建单例
user_service = UserService()
