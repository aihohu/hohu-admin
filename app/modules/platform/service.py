from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import STATUS_ENABLED
from app.core.exceptions import AuthenticationException
from app.core.security import create_platform_access_token, verify_password
from app.modules.platform.models import PlatformPrincipal
from app.modules.platform.schemas import PlatformLoginCredentials

_DUMMY_PASSWORD_HASH = "$2b$12$iJEqWB.R2W5IY4FyTi8TUO556esQFdl6ud7yG59tB/vzZaaTfO3ym"


def _password_matches(password: str, hashed_password: str) -> bool:
    try:
        return verify_password(password, hashed_password)
    except (TypeError, ValueError):
        return False


class PlatformAuthService:
    async def authenticate(
        self, db: AsyncSession, credentials: PlatformLoginCredentials
    ) -> str:
        principal = await db.scalar(
            select(PlatformPrincipal).where(
                PlatformPrincipal.principal_name == credentials.principal_name
            )
        )
        password_hash = principal.hashed_password if principal else _DUMMY_PASSWORD_HASH
        password_valid = _password_matches(credentials.password, password_hash)
        if (
            principal is None
            or not password_valid
            or principal.status != STATUS_ENABLED
        ):
            raise AuthenticationException(
                "平台账号或密码错误", error_code="PLATFORM_INVALID_CREDENTIALS"
            )
        principal.last_login_at = datetime.now(UTC)
        await db.flush()
        return create_platform_access_token(
            subject=str(principal.principal_id),
            principal_version=principal.row_version,
        )


platform_auth_service = PlatformAuthService()
