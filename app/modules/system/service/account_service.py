"""Account mutations for public auth, kept inside the system model boundary."""

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.exceptions import BusinessRuleException
from app.core.security import get_password_hash, verify_password
from app.modules.system.models.role import Role
from app.modules.system.models.user import User


class AccountService:
    async def find(self, db, tenant_id, username, *, lock=False):
        stmt = select(User).where(
            User.tenant_id == tenant_id, User.user_name == username
        )
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        return await db.scalar(stmt)

    async def registration_role(self, db, tenant_id, role_id, *, lock=False):
        if not role_id:
            return None
        if not str(role_id).isascii() or not str(role_id).isdigit():
            raise BusinessRuleException(
                "Invalid registration role", error_code="REGISTRATION_ROLE_INVALID"
            )
        if not 0 < int(role_id) < 2**63:
            raise BusinessRuleException(
                "Invalid registration role", error_code="REGISTRATION_ROLE_INVALID"
            )
        stmt = select(Role).where(
            Role.tenant_id == tenant_id, Role.role_id == int(role_id)
        )
        if lock:
            stmt = stmt.with_for_update().execution_options(populate_existing=True)
        role = await db.scalar(stmt)
        if role is None or role.status != "1" or role.role_code == "R_SUPER":
            raise BusinessRuleException(
                "Invalid registration role", error_code="REGISTRATION_ROLE_INVALID"
            )
        if any(
            (m.permission or "").startswith(("system:", "platform:"))
            or (m.permission or "") == "*"
            or (m.route_name or "").startswith(
                ("system", "tenant", "ai_provider", "ai_agent")
            )
            for m in role.menus
        ):
            raise BusinessRuleException(
                "Management roles cannot be assigned publicly",
                error_code="REGISTRATION_ROLE_INVALID",
            )
        return role

    async def role_options(self, db, tenant_id):
        roles = (
            await db.scalars(
                select(Role).where(Role.tenant_id == tenant_id, Role.status == "1")
            )
        ).all()
        result = []
        for role in roles:
            try:
                await self.registration_role(db, tenant_id, str(role.role_id))
            except BusinessRuleException:
                continue
            result.append({"label": role.role_name, "value": str(role.role_id)})
        return result

    async def register(self, db, tenant_id, payload, role_id):
        role = await self.registration_role(db, tenant_id, role_id, lock=True)
        user = User(
            tenant_id=tenant_id,
            user_name=payload.user_name,
            nickname=payload.user_name,
            user_email=str(payload.email),
            email_verified_at=datetime.now(UTC),
            hashed_password=get_password_hash(payload.password),
            status="1",
            roles=[],
        )
        try:
            async with db.begin_nested():
                if role:
                    user.roles = [role]
                db.add(user)
                await db.flush()
        except IntegrityError:
            raise BusinessRuleException(
                "Registration could not be completed",
                error_code="REGISTRATION_CONFLICT",
            ) from None
        return user

    def recovery_identity(self, user, email):
        if (
            user is None
            or user.status != "1"
            or not user.email_verified_at
            or user.user_email != email
        ):
            return None
        return f"{user.user_id}:{user.auth_version}:{user.email_verified_at.isoformat()}:{email}"

    def verify_current_password(self, user, password):
        if (
            user is None
            or user.status != "1"
            or not verify_password(password, user.hashed_password)
        ):
            raise BusinessRuleException(
                "Current password is incorrect", error_code="INCORRECT_OLD_PASSWORD"
            )

    async def bind(self, db, user, email):
        old = user.user_email if user.email_verified_at else None
        user.user_email = email
        user.email_verified_at = None
        await db.flush()  # DB trigger invalidates changed addresses for every writer.
        user.email_verified_at = datetime.now(UTC)
        await db.flush()
        return old

    async def reset(self, db, user, password):
        user.hashed_password = get_password_hash(password)
        user.auth_version += 1
        await db.flush()


account_service = AccountService()
