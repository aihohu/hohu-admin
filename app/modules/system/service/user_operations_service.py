"""User-domain operations shared by authenticated HTTP and AI adapters."""

from collections.abc import Sequence

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.tenant import TenantContext
from app.core.tenant_scope import tenant_filter
from app.modules.system.models.user import User
from app.modules.system.schemas.user import UserCreate
from app.modules.system.service.user_department_assignment_service import (
    user_department_assignment_service,
)
from app.modules.system.service.user_role_assignment_service import (
    user_role_assignment_service,
)
from app.modules.system.service.user_service import user_service


class UserOperationsService:
    """Use caller-owned transactions and existing assignment authorization."""

    async def count_users(
        self,
        db: AsyncSession,
        *,
        tenant: TenantContext,
        scope_filters: Sequence[ColumnElement[bool]],
        status: str | None = None,
        user_gender: str | None = None,
    ) -> int:
        """Count within both the trusted tenant and server-resolved row scope."""
        stmt = select(func.pg_catalog.count(User.user_id)).where(
            tenant_filter(User, tenant=tenant), *scope_filters
        )
        if status is not None:
            stmt = stmt.where(User.status == status)
        if user_gender is not None:
            stmt = stmt.where(User.user_gender == user_gender)
        return int(await db.scalar(stmt) or 0)

    async def create_user(
        self,
        db: AsyncSession,
        user_in: UserCreate,
        *,
        actor_user_id: int,
        tenant: TenantContext,
    ) -> User:
        """Create and assign one user without committing or handling rollback."""
        await user_role_assignment_service.ensure_create_permissions(
            db,
            actor_user_id=actor_user_id,
            explicit_roles=user_in.role_ids is not None,
            tenant=tenant,
        )
        await user_department_assignment_service.ensure_create_permissions(
            db,
            actor_user_id=actor_user_id,
            has_departments=bool(user_in.dept_ids),
            tenant=tenant,
        )
        new_user = await user_service.create_user(db, user_in, tenant=tenant)
        await db.flush()
        await user_role_assignment_service.assign_created_user_roles(
            db,
            actor_user_id=actor_user_id,
            target_user_id=new_user.user_id,
            role_ids=user_in.role_ids,
            dept_ids=[int(item.dept_id) for item in user_in.dept_ids],
            tenant=tenant,
        )
        await user_department_assignment_service.assign_created_user_departments(
            db,
            actor_user_id=actor_user_id,
            target_user_id=new_user.user_id,
            dept_assignments=[
                (int(item.dept_id), item.is_primary) for item in user_in.dept_ids
            ],
            tenant=tenant,
        )
        return new_user


user_operations_service = UserOperationsService()
