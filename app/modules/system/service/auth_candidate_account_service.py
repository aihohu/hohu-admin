"""System model boundary for explicit Auth candidates; never commits or hashes."""

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select

from app.core.authorization_plan import LockPlan, NewResource, ResourceKey, ResourceKind
from app.core.exceptions import AuthenticationException, BusinessException
from app.core.security import decrypt_value
from app.db.authorization_plan_session import _order_error
from app.db.base import role_menus
from app.modules.system.models.mail_config import MailConfig
from app.modules.system.models.menu import Menu
from app.modules.system.models.role import Role
from app.modules.system.models.setting import SystemSetting
from app.modules.system.models.tenant import Tenant
from app.modules.system.models.user import User
from app.modules.system.service.authorization_transaction_service import _Transaction
from app.modules.system.service.mail_service import MailSettings, ready
from app.modules.system.service.settings_service import _decode
from app.modules.system.settings_catalog import SETTINGS

_ACCOUNT_KEYS = (
    "register_enabled",
    "password_reset_enabled",
    "email_code_minutes",
    "registration_role_id",
)


def _error(code, message="Account authorization changed", status=409):
    return BusinessException(status, message, error_code=code)


@dataclass(frozen=True, slots=True)
class Identity:
    tenant_id: int
    tenant_code: str
    tenant_version: int
    user_id: int
    user_version: int


@dataclass(frozen=True, slots=True)
class Account:
    user_id: int
    tenant_id: int
    user_name: str
    hashed_password: str = field(repr=False)
    status: str
    auth_version: int
    user_email: str | None
    email_verified_at: datetime | None
    update_time: datetime | None


@dataclass(frozen=True, slots=True)
class AccountState:
    tenant: tuple
    account: Account | None
    settings: tuple = ()
    mail_version: int = 0
    mail: MailSettings | None = field(default=None, repr=False)
    role: tuple | None = None
    menus: tuple = ()
    read_rows: int = field(init=False)
    decoded_bytes: int = field(init=False)

    def __post_init__(self):
        decoded = len(
            json.dumps(
                [
                    self.tenant,
                    asdict(self.account) if self.account else None,
                    self.settings,
                    self.mail.model_dump() if self.mail else None,
                    self.role,
                    self.menus,
                ],
                default=str,
            ).encode()
        )
        if decoded > 64 * 1024 * 1024:
            raise _error("AUTHORIZATION_PLAN_LIMIT_EXCEEDED", status=422)
        object.__setattr__(self, "decoded_bytes", decoded)
        object.__setattr__(
            self,
            "read_rows",
            1
            + int(self.account is not None)
            + len(self.settings)
            + int(self.mail_version != 0)
            + int(self.role is not None)
            + len(self.menus),
        )

    @property
    def tenant_id(self):
        return self.tenant[0]

    def value(self, key):
        stored = next(
            (r[2] for r in self.settings if r[1] == key and r[3] == "1"),
            SETTINGS[key].default,
        )
        return _decode(SETTINGS[key], stored)

    def identity(self):
        if self.account is None or self.account.status != "1":
            raise AuthenticationException(error_code="INVALID_CREDENTIALS")
        return Identity(
            self.tenant_id,
            self.tenant[1],
            self.tenant[2],
            self.account.user_id,
            self.account.auth_version,
        )


class AuthCandidateAccountService:
    async def tenant_id(self, db, code):
        row = await db.scalar(
            select(Tenant.tenant_id).where(Tenant.tenant_code == code)
        )
        if row is None:
            raise AuthenticationException(error_code="INVALID_CREDENTIALS")
        return row

    async def read(self, db, tenant_id, *, user_name=None, user_id=None, policy=False):
        tenant = (
            await db.execute(
                select(
                    Tenant.tenant_id,
                    Tenant.tenant_code,
                    Tenant.row_version,
                    Tenant.status,
                ).where(Tenant.tenant_id == tenant_id)
            )
        ).one_or_none()
        if tenant is None or tenant.status != "1":
            raise AuthenticationException(error_code="INVALID_CREDENTIALS")
        account = None
        if user_name is not None or user_id is not None:
            columns = tuple(
                User.__table__.c[name] for name in Account.__dataclass_fields__
            )
            statement = select(*columns).where(User.tenant_id == tenant_id)
            statement = (
                statement.where(User.user_id == user_id)
                if user_id is not None
                else statement.where(User.user_name == user_name)
            )
            row = (await db.execute(statement)).mappings().one_or_none()
            if row is not None:
                account = Account(**row)
        if not policy:
            return AccountState(tuple(tenant), account)
        rows = tuple(
            tuple(row)
            for row in (
                await db.execute(
                    select(
                        SystemSetting.setting_id,
                        SystemSetting.setting_key,
                        SystemSetting.setting_value,
                        SystemSetting.status,
                    )
                    .where(
                        SystemSetting.tenant_id == tenant_id,
                        SystemSetting.setting_key.in_(_ACCOUNT_KEYS),
                    )
                    .order_by(SystemSetting.setting_id)
                    .limit(5)
                )
            ).all()
        )
        mail_row = (
            await db.execute(
                select(
                    MailConfig.version,
                    MailConfig.values,
                    MailConfig.password_ciphertext,
                ).where(MailConfig.config_id == 1)
            )
        ).one_or_none()
        # Fixed row count and decoded bound before processing stored JSON/config.
        if (
            len(rows) > 4
            or len(
                json.dumps(
                    [rows, tuple(mail_row) if mail_row else None], default=str
                ).encode()
            )
            > 65536
        ):
            raise _error("AUTHORIZATION_PLAN_LIMIT_EXCEEDED", status=422)
        mail = MailSettings()
        if mail_row:
            mail = MailSettings(
                **dict(
                    mail_row[1],
                    password=decrypt_value(mail_row[2]) if mail_row[2] else "",
                )
            )
        state = AccountState(
            tuple(tenant), account, rows, mail_row[0] if mail_row else 0, mail
        )
        role_id = state.value("registration_role_id")
        role, menus = None, ()
        if role_id:
            if (
                not str(role_id).isascii()
                or not str(role_id).isdigit()
                or not 0 < int(role_id) < 2**63
            ):
                raise _error("REGISTRATION_ROLE_INVALID", status=400)
            role = (
                await db.execute(
                    select(Role.role_id, Role.status, Role.role_code).where(
                        Role.tenant_id == tenant_id, Role.role_id == int(role_id)
                    )
                )
            ).one_or_none()
            menus = tuple(
                tuple(row)
                for row in (
                    await db.execute(
                        select(
                            Menu.menu_id,
                            Menu.permission,
                            Menu.route_name,
                        )
                        .join(
                            role_menus,
                            (role_menus.c.menu_id == Menu.menu_id)
                            & (role_menus.c.tenant_id == Menu.tenant_id),
                        )
                        .where(
                            Menu.tenant_id == tenant_id,
                            role_menus.c.tenant_id == tenant_id,
                            role_menus.c.role_id == int(role_id),
                        )
                        .order_by(Menu.menu_id)
                        .limit(4097)
                    )
                ).all()
            )
            if len(menus) > 4096 or len(json.dumps(menus).encode()) > 1024 * 1024:
                raise _error("AUTHORIZATION_PLAN_LIMIT_EXCEEDED", status=422)
        return AccountState(
            tuple(tenant),
            account,
            rows,
            state.mail_version,
            mail,
            tuple(role) if role else None,
            menus,
        )

    def policy(self, state, purpose):
        if purpose in {"register", "reset"}:
            key = (
                "register_enabled"
                if purpose == "register"
                else "password_reset_enabled"
            )
            if not state.value(key):
                raise _error("ACCOUNT_SERVICE_DISABLED", status=400)
        if state.mail is None or not ready(state.mail):
            raise _error("MAIL_NOT_CONFIGURED", status=400)
        if purpose == "register" and state.value("registration_role_id"):
            if (
                state.role is None
                or state.role[1] != "1"
                or state.role[2] == "R_SUPER"
                or any(
                    (permission or "").startswith(("system:", "platform:"))
                    or permission == "*"
                    or (route or "").startswith(
                        ("system", "tenant", "ai_provider", "ai_agent")
                    )
                    for _, permission, route in state.menus
                )
            ):
                raise _error("REGISTRATION_ROLE_INVALID", status=400)

    def plan(self, state, *, new_user_id=None):
        tid = state.tenant_id
        existing = [
            ResourceKey(ResourceKind.SETTING, tid, r[1]) for r in state.settings
        ]
        if state.account:
            existing.append(ResourceKey(ResourceKind.USER, tid, state.account.user_id))
        if state.role:
            existing.append(ResourceKey(ResourceKind.ROLE, tid, state.role[0]))
            existing.extend(
                ResourceKey(ResourceKind.ROLE_MENUS, tid, (state.role[0], m[0]))
                for m in state.menus
            )
        new = []
        if new_user_id is not None:
            new.append(NewResource(ResourceKey(ResourceKind.USER, tid, new_user_id)))
            if state.role:
                new.append(
                    NewResource(
                        ResourceKey(
                            ResourceKind.USER_ROLES, tid, (new_user_id, state.role[0])
                        )
                    )
                )
        return LockPlan(existing=tuple(existing), new=tuple(new))

    async def apply(
        self, tx, state, operation, *, payload, password_hash=None, new_user_id=None
    ):
        if type(tx) is not _Transaction:
            raise _order_error()
        tx.assert_held()
        tid = state.tenant_id
        self.policy(state, operation)
        tx.assert_held(
            *(ResourceKey(ResourceKind.SETTING, tid, r[1]) for r in state.settings)
        )
        async with tx.aggregate(tid):
            if operation == "register":
                if state.account is not None:
                    raise _error("REGISTRATION_CONFLICT", status=400)
                if state.role:
                    tx.assert_held(ResourceKey(ResourceKind.ROLE, tid, state.role[0]))
                key = ResourceKey(ResourceKind.USER, tid, new_user_id)
                await tx.insert_reserved(
                    key,
                    {
                        "user_name": payload.user_name,
                        "nickname": payload.user_name,
                        "user_email": str(payload.email),
                        "email_verified_at": datetime.now(UTC),
                        "hashed_password": password_hash,
                        "status": "1",
                        "auth_version": 1,
                    },
                )
                if state.role:
                    await tx.insert_reserved(
                        ResourceKey(
                            ResourceKind.USER_ROLES, tid, (new_user_id, state.role[0])
                        ),
                        {},
                    )
                return str(new_user_id)
            account = state.account
            if account is None:
                raise AuthenticationException(error_code="INVALID_CREDENTIALS")
            key = ResourceKey(ResourceKind.USER, tid, account.user_id)
            tx.assert_held(key)
            if operation == "reset":
                await tx.update_held(
                    key,
                    {
                        "hashed_password": password_hash,
                        "auth_version": account.auth_version + 1,
                    },
                    expected={"auth_version": account.auth_version},
                )
            elif operation == "bind":
                await tx.update_held(
                    key,
                    {"user_email": str(payload.email), "email_verified_at": None},
                    expected={"auth_version": account.auth_version},
                )
                await tx.update_held(key, {"email_verified_at": datetime.now(UTC)})
            else:
                raise _error("AUTHORIZATION_CANDIDATE_OPERATION_INVALID", status=400)
        return None


auth_candidate_accounts = AuthCandidateAccountService()
