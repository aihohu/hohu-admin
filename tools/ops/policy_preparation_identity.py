"""Credentials and sessions for the loopback preparation controller only."""

import asyncio
import hashlib
import secrets
import time
from collections import deque

from pydantic import Field, ValidationError
from sqlalchemy import select, text

from app.core.exceptions import BusinessException
from app.core.security import verify_password
from app.core.tenant import TenantContext, require_tenant_runtime_enabled
from app.db.base import role_menus, user_roles
from app.modules.auth.schemas.auth import LoginCredentials
from app.modules.auth.service import _DUMMY_PASSWORD_HASH, AccessIdentity, auth_service
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User
from app.modules.system.schemas.field_management import Closed


def invalid():
    return BusinessException(
        401, "准备身份无效或已过期", error_code="INVALID_CREDENTIALS"
    )


class PreparationLogin(Closed):
    tenant_code: str = Field(default="default", min_length=2, max_length=32)
    user_name: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=72, repr=False)


class PreparationSessions:
    def __init__(self):
        self.tokens = {}
        self.attempts = deque(maxlen=10)

    def attempt(self):
        now = time.monotonic()
        while self.attempts and self.attempts[0] <= now - 60:
            self.attempts.popleft()
        if len(self.attempts) >= 10:
            raise BusinessException(
                429, "准备登录过于频繁", error_code="RATE_LIMIT_EXCEEDED"
            )
        self.attempts.append(now)

    def issue(self, identity):
        now = time.monotonic()
        self.tokens = {k: v for k, v in self.tokens.items() if v[0] > now}
        if len(self.tokens) >= 16:
            raise BusinessException(
                429, "准备会话数量超限", error_code="RATE_LIMIT_EXCEEDED"
            )
        token = secrets.token_urlsafe(32)
        self.tokens[hashlib.sha256(token.encode()).digest()] = (now + 900, identity)
        return token

    def resolve(self, token):
        value = self.tokens.get(hashlib.sha256(token.encode()).digest())
        if value is None or value[0] <= time.monotonic():
            raise invalid()
        return value[1]

    def revoke(self, token):
        self.tokens.pop(hashlib.sha256(token.encode()).digest(), None)

    def clear(self):
        self.tokens.clear()


async def credential_identity(sessions, payload):
    try:
        credentials = PreparationLogin.model_validate(payload)
    except ValidationError:
        raise invalid() from None
    if len(credentials.password.encode()) > 72:
        raise invalid()
    found = None
    async with asyncio.timeout(5):
        async with sessions() as db:
            try:
                await db.execute(text("SET LOCAL statement_timeout = '2s'"))
                tenant = await auth_service.resolve_login_tenant(
                    LoginCredentials(
                        userName=credentials.user_name,
                        password=credentials.password,
                        tenantCode=credentials.tenant_code,
                    ),
                    db,
                )
                require_tenant_runtime_enabled(tenant.tenant_id, surface="login")
                row = (
                    await db.execute(
                        select(
                            User.user_id,
                            User.auth_version,
                            User.hashed_password,
                            User.status,
                        ).where(
                            User.tenant_id == tenant.tenant_id,
                            User.user_name == credentials.user_name,
                        )
                    )
                ).one_or_none()
                if row is not None and row.status == "1":
                    found = (
                        AccessIdentity(
                            TenantContext(
                                tenant.tenant_id,
                                tenant.tenant_code,
                                row.user_id,
                                tenant.row_version,
                                "access_token",
                            ),
                            row.auth_version,
                        ),
                        row.hashed_password,
                    )
            except BusinessException:
                found = None
            finally:
                await db.rollback()
        # No SQL transaction or authorization lock survives this CPU boundary.
        try:
            matches = await asyncio.to_thread(
                verify_password,
                credentials.password,
                found[1] if found else _DUMMY_PASSWORD_HASH,
            )
        except (ValueError, TypeError):
            matches = False
    if not found or not matches:
        raise invalid()
    return found[0]


async def require_preparation_administrators(sessions):
    """No generic head or marker may be created before the upgrade has an administrator."""
    async with asyncio.timeout(5):
        async with sessions() as db:
            await db.execute(text("SET LOCAL statement_timeout = '2s'"))
            base_roles = (
                select(Role.role_id)
                .join(
                    user_roles,
                    (user_roles.c.role_id == Role.role_id)
                    & (user_roles.c.tenant_id == Role.tenant_id),
                )
                .where(
                    user_roles.c.user_id == User.user_id,
                    Role.tenant_id == User.tenant_id,
                    Role.status == "1",
                )
            )
            predicates = [base_roles.where(Role.role_code == "R_SUPER").exists()]
            for suffix in ("view", "edit", "activate"):
                predicates.append(
                    base_roles.join(
                        role_menus,
                        (role_menus.c.role_id == Role.role_id)
                        & (role_menus.c.tenant_id == Role.tenant_id),
                    )
                    .join(
                        Menu,
                        (Menu.menu_id == role_menus.c.menu_id)
                        & (Menu.tenant_id == Role.tenant_id),
                    )
                    .where(
                        Menu.permission == f"system:role:field-policy-{suffix}",
                    )
                    .exists()
                )
            manageable = (
                select(User.user_id)
                .where(
                    User.tenant_id == Tenant.tenant_id, User.status == "1", *predicates
                )
                .exists()
            )
            rows = (
                await db.execute(
                    select(Tenant.tenant_id, manageable.label("manageable"))
                    .where(Tenant.status == "1")
                    .order_by(Tenant.tenant_id)
                    .limit(1025)
                )
            ).all()
            await db.rollback()
    if not rows or len(rows) > 1024 or any(not row.manageable for row in rows):
        raise BusinessException(
            409,
            "升级前须为每个启用租户配置字段策略专用管理权限",
            error_code="FIELD_POLICY_ADMIN_PREFLIGHT_REQUIRED",
        )
