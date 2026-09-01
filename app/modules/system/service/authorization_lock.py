"""Deterministic locking primitives for authorization aggregate writers."""

from dataclasses import dataclass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.core.exceptions import BusinessRuleException
from app.core.tenant import TenantContext
from app.modules.system.models.dept import Dept
from app.modules.system.models.role import Role
from app.modules.system.models.user import User


@dataclass(frozen=True)
class AuthorizationLockSet:
    """Normalized IDs held by the current caller-owned transaction."""

    role_ids: tuple[int, ...]
    dept_ids: tuple[int, ...]
    user_ids: tuple[int, ...]


def _normalize_ids(values: list[int] | tuple[int, ...] | set[int]) -> tuple[int, ...]:
    normalized = {int(value) for value in values}
    if any(value <= 0 for value in normalized):
        raise BusinessRuleException(
            "授权锁目标无效",
            error_code="AUTHORIZATION_SNAPSHOT_STALE",
        )
    return tuple(sorted(normalized))


class AuthorizationLockService:
    """Acquire role, department, then user row locks in one stable order."""

    MIGRATION_ADVISORY_LOCK_KEY = 0x484F485541555448

    async def _lock_ids(
        self,
        db: AsyncSession,
        *,
        model: type[Role] | type[Dept] | type[User],
        id_column,
        ids: tuple[int, ...],
        tenant: TenantContext,
    ) -> tuple[int, ...]:
        if not ids:
            return ()
        result = await db.execute(
            select(id_column)
            .where(
                model.tenant_id == tenant.tenant_id,
                id_column.in_(ids),
            )
            .order_by(id_column.asc())
            .with_for_update()
        )
        locked = tuple(int(value) for value in result.scalars())
        if locked != ids:
            raise BusinessRuleException(
                f"{model.__tablename__} 授权事实已变化",
                error_code="AUTHORIZATION_SNAPSHOT_STALE",
            )
        return locked

    async def lock_targets(
        self,
        db: AsyncSession,
        *,
        role_ids: list[int] | tuple[int, ...] | set[int],
        dept_ids: list[int] | tuple[int, ...] | set[int],
        user_ids: list[int] | tuple[int, ...] | set[int],
        tenant: TenantContext,
    ) -> AuthorizationLockSet:
        """Lock all known targets without starting or committing a transaction."""
        normalized_roles = _normalize_ids(role_ids)
        normalized_depts = _normalize_ids(dept_ids)
        normalized_users = _normalize_ids(user_ids)
        locked_roles = await self._lock_ids(
            db,
            model=Role,
            id_column=Role.role_id,
            ids=normalized_roles,
            tenant=tenant,
        )
        locked_depts = await self._lock_ids(
            db,
            model=Dept,
            id_column=Dept.dept_id,
            ids=normalized_depts,
            tenant=tenant,
        )
        locked_users = await self._lock_ids(
            db,
            model=User,
            id_column=User.user_id,
            ids=normalized_users,
            tenant=tenant,
        )
        return AuthorizationLockSet(
            role_ids=locked_roles,
            dept_ids=locked_depts,
            user_ids=locked_users,
        )

    async def lock_authorization_migration(self, db: AsyncSession) -> None:
        """Hold the global migration lock until the caller transaction ends."""
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": self.MIGRATION_ADVISORY_LOCK_KEY},
        )

    async def lock_authorization_migration_session(
        self,
        connection: AsyncConnection,
    ) -> None:
        """Acquire a session lock; the caller owns its transaction lifecycle."""
        await connection.execute(
            text("SELECT pg_advisory_lock(:lock_key)"),
            {"lock_key": self.MIGRATION_ADVISORY_LOCK_KEY},
        )

    async def unlock_authorization_migration_session(
        self,
        connection: AsyncConnection,
    ) -> None:
        """Release the caller-owned session lock and fail closed if it was lost."""
        released = await connection.scalar(
            text("SELECT pg_advisory_unlock(:lock_key)"),
            {"lock_key": self.MIGRATION_ADVISORY_LOCK_KEY},
        )
        if released is not True:
            raise BusinessRuleException(
                "授权迁移维护锁已丢失",
                error_code="AUTHORIZATION_MIGRATION_LOCK_LOST",
            )


authorization_lock_service = AuthorizationLockService()
