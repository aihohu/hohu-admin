"""Independent platform token authentication with live principal revalidation."""

import re
from dataclasses import dataclass

from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import STATUS_ENABLED
from app.core.config import settings
from app.core.exceptions import AuthenticationException, AuthorizationException
from app.modules.platform.constants import ASSIGNABLE_PLATFORM_PERMISSIONS
from app.modules.platform.models import PlatformPrincipal

_POSITIVE_ID_RE = re.compile(r"^[1-9][0-9]*$")


@dataclass(frozen=True, slots=True)
class AuthenticatedPlatformPrincipal:
    principal_id: int
    principal_name: str
    permissions: frozenset[str]


def _invalid_platform_token() -> AuthenticationException:
    return AuthenticationException(
        "平台 Token 无效或已过期", error_code="PLATFORM_TOKEN_INVALID"
    )


async def authenticate_platform_token(
    token: str, db: AsyncSession
) -> AuthenticatedPlatformPrincipal:
    """Authenticate only a platform token; tenant tokens never imply platform power."""
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM]
        )
    except JWTError as exc:
        raise _invalid_platform_token() from exc

    token_type = payload.get("type")
    if token_type in {"access", "refresh"}:
        raise AuthorizationException(
            "当前身份不是平台管理员",
            error_code="PLATFORM_ADMIN_REQUIRED",
        )
    principal_id_claim = payload.get("sub")
    version_claim = payload.get("pver")
    if (
        token_type != "platform_access"
        or "tid" in payload
        or not isinstance(principal_id_claim, str)
        or _POSITIVE_ID_RE.fullmatch(principal_id_claim) is None
        or not isinstance(version_claim, str)
        or _POSITIVE_ID_RE.fullmatch(version_claim) is None
    ):
        raise _invalid_platform_token()

    principal_id = int(principal_id_claim)
    version = int(version_claim)
    principal = await db.scalar(
        select(PlatformPrincipal)
        .where(PlatformPrincipal.principal_id == principal_id)
        .with_for_update(read=True)
    )
    if principal is None or principal.row_version != version:
        raise _invalid_platform_token()
    if principal.status != STATUS_ENABLED:
        raise AuthorizationException(
            "平台身份已被禁用", error_code="PLATFORM_PRINCIPAL_DISABLED"
        )
    raw_permissions = principal.permissions
    if (
        not isinstance(raw_permissions, list)
        or not raw_permissions
        or any(
            not isinstance(permission, str)
            or permission not in ASSIGNABLE_PLATFORM_PERMISSIONS
            for permission in raw_permissions
        )
    ):
        raise AuthenticationException(
            "平台身份配置无效", error_code="PLATFORM_PRINCIPAL_INVALID"
        )
    return AuthenticatedPlatformPrincipal(
        principal_id=principal.principal_id,
        principal_name=principal.principal_name,
        permissions=frozenset(raw_permissions),
    )
