"""Principal-only identity and password preparation, separate from tenant users."""

import asyncio
import weakref
from dataclasses import dataclass, field
from datetime import UTC, datetime

from jose import JWTError, jwt
from sqlalchemy import select

from app.core.authorization_io import verify_password
from app.core.authorization_plan import (
    LockPlan,
    ResourceKey,
    ResourceKind,
    TransactionBudget,
)
from app.core.exceptions import AuthenticationException, AuthorizationException
from app.core.security import (
    create_platform_access_token,
    decode_platform_access_token,
    decode_tenant_token,
)
from app.db.authorization_plan_session import _order_error
from app.db.authorization_system_owner import require_candidate_session
from app.modules.platform.constants import (
    ASSIGNABLE_PLATFORM_PERMISSIONS,
    PLATFORM_PRINCIPAL_NAME_RE,
)
from app.modules.platform.models import PlatformPrincipal
from app.modules.system.service.authorization_read_budget import (
    bounded_authorization_reads,
)
from app.modules.system.service.authorization_transaction_service import (
    authorization_transactions,
)

_PREPARATIONS = weakref.WeakKeyDictionary()
_DUMMY = "$2b$12$iJEqWB.R2W5IY4FyTi8TUO556esQFdl6ud7yG59tB/vzZaaTfO3ym"


def _invalid():
    return AuthenticationException(
        "平台身份无效或已过期", error_code="PLATFORM_TOKEN_INVALID"
    )


@dataclass(frozen=True, slots=True)
class PrincipalIdentity:
    principal_id: int
    principal_version: int

    def __post_init__(self):
        if any(
            type(value) is not int or not 0 < value < 2**63
            for value in (self.principal_id, self.principal_version)
        ):
            raise _invalid()


@dataclass(frozen=True, slots=True)
class PrincipalSnapshot:
    identity: PrincipalIdentity
    principal_name: str
    permissions: tuple[str, ...]
    keys: tuple[ResourceKey, ...]


async def read_principal(db, identity, permission=None):
    if type(identity) is not PrincipalIdentity:
        raise _invalid()
    row = (
        (
            await db.execute(
                select(PlatformPrincipal.__table__).where(
                    PlatformPrincipal.principal_id == identity.principal_id
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if row is None or row["row_version"] != identity.principal_version:
        raise _invalid()
    if row["status"] != "1":
        raise AuthorizationException(
            "平台身份已被禁用", error_code="PLATFORM_PRINCIPAL_DISABLED"
        )
    permissions = row["permissions"]
    if (
        type(permissions) is not list
        or not permissions
        or any(
            type(item) is not str or item not in ASSIGNABLE_PLATFORM_PERMISSIONS
            for item in permissions
        )
    ):
        raise AuthenticationException(
            "平台身份配置无效", error_code="PLATFORM_PRINCIPAL_INVALID"
        )
    if permission is not None and permission not in permissions:
        raise AuthorizationException(
            "缺少平台权限", error_code="PLATFORM_PERMISSION_REQUIRED"
        )
    return PrincipalSnapshot(
        identity,
        row["principal_name"],
        tuple(permissions),
        (ResourceKey(ResourceKind.PLATFORM_PRINCIPAL, None, (identity.principal_id,)),),
    )


async def assert_principal(db, snapshot):
    if type(snapshot) is not PrincipalSnapshot:
        raise _invalid()
    current = await read_principal(db, snapshot.identity)
    if current != snapshot:
        raise _invalid()
    return current


def token_identity(token):
    if type(token) is not str or len(token) > 16384:
        raise _invalid()
    try:
        token_type = jwt.get_unverified_claims(token).get("type")
        if token_type in {"access", "refresh"}:
            decode_tenant_token(token)
            raise AuthorizationException(
                "当前身份不是平台管理员", error_code="PLATFORM_ADMIN_REQUIRED"
            )
        payload = decode_platform_access_token(token)
        if token_type != "platform_access" or "tid" in payload:
            raise _invalid()
        for name in ("sub", "pver"):
            value = payload.get(name)
            if (
                type(value) is not str
                or not 1 <= len(value) <= 19
                or not value.isascii()
                or not value.isdigit()
                or value.startswith("0")
            ):
                raise _invalid()
        return PrincipalIdentity(int(payload["sub"]), int(payload["pver"]))
    except (JWTError, TypeError, ValueError):
        raise _invalid() from None


@dataclass(frozen=True, slots=True, eq=False, weakref_slot=True)
class PasswordPreparation:
    identity: PrincipalIdentity
    principal_name: str
    password_hash: str = field(repr=False)
    permissions: tuple = field(repr=False)
    _session: object = field(repr=False)


async def prepare_password(db, *, principal_name):
    require_candidate_session(db)
    if (
        type(principal_name) is not str
        or PLATFORM_PRINCIPAL_NAME_RE.fullmatch(principal_name) is None
    ):
        raise _invalid()
    with bounded_authorization_reads(db):
        async with asyncio.timeout(5):
            row = (
                (
                    await db.execute(
                        select(PlatformPrincipal.__table__).where(
                            PlatformPrincipal.principal_name == principal_name
                        )
                    )
                )
                .mappings()
                .one_or_none()
            )
    if row is None or row["status"] != "1":
        return None
    preparation = PasswordPreparation(
        PrincipalIdentity(row["principal_id"], row["row_version"]),
        row["principal_name"],
        row["hashed_password"],
        tuple(row["permissions"]),
        weakref.ref(db),
    )
    _PREPARATIONS[preparation] = "read"
    return preparation


async def verify_preparation(preparation, password):
    if type(password) is not str or not password or len(password.encode()) > 72:
        raise _invalid()
    if preparation is not None:
        if (
            type(preparation) is not PasswordPreparation
            or _PREPARATIONS.get(preparation) != "read"
        ):
            raise _order_error()
        db = preparation._session()
        if db is not None and db.in_transaction():
            raise _order_error()
        _PREPARATIONS[preparation] = "checking"
    try:
        valid = await verify_password(
            password, preparation.password_hash if preparation else _DUMMY
        )
    except (TypeError, ValueError):
        valid = False
    if not valid or preparation is None:
        raise AuthenticationException(
            "平台账号或密码错误", error_code="PLATFORM_INVALID_CREDENTIALS"
        )
    _PREPARATIONS[preparation] = "verified"


async def check_password_preparation(db, preparation):
    if (
        type(preparation) is not PasswordPreparation
        or _PREPARATIONS.get(preparation) != "verified"
    ):
        raise _order_error()
    row = (
        (
            await db.execute(
                select(PlatformPrincipal.__table__).where(
                    PlatformPrincipal.principal_id == preparation.identity.principal_id
                )
            )
        )
        .mappings()
        .one_or_none()
    )
    if (
        row is None
        or row["row_version"] != preparation.identity.principal_version
        or row["hashed_password"] != preparation.password_hash
        or row["status"] != "1"
        or tuple(row["permissions"]) != preparation.permissions
        or row["principal_name"] != preparation.principal_name
    ):
        raise _invalid()
    return row


class PrincipalAuthorizationCandidate:
    async def login(self, db, *, preparation):
        require_candidate_session(db)
        key = ResourceKey(
            ResourceKind.PLATFORM_PRINCIPAL, None, (preparation.identity.principal_id,)
        )
        with bounded_authorization_reads(db):
            async with authorization_transactions.scope(
                db, budget=TransactionBudget(timeout_ms=8000)
            ) as tx:
                await tx.lock(LockPlan(existing=(key,)))
                await check_password_preparation(db, preparation)
                await tx.update_metadata(
                    key,
                    {"last_login_at": datetime.now(UTC)},
                    expected={"row_version": preparation.identity.principal_version},
                )
        _PREPARATIONS[preparation] = "consumed"
        return create_platform_access_token(
            subject=str(preparation.identity.principal_id),
            principal_version=preparation.identity.principal_version,
        )

    async def current(self, db, *, token):
        require_candidate_session(db)
        identity = token_identity(token)
        with bounded_authorization_reads(db):
            async with authorization_transactions.scope(
                db, budget=TransactionBudget(timeout_ms=8000)
            ) as tx:
                snapshot = await read_principal(db, identity)
                await tx.lock(LockPlan(existing=snapshot.keys))
                return await assert_principal(db, snapshot)


principal_authorization_candidate = PrincipalAuthorizationCandidate()
