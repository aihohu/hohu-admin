"""Offline bootstrap for the first independent platform principal."""

import argparse
import asyncio
from getpass import getpass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import get_password_hash
from app.db.session import AsyncSessionLocal
from app.modules.platform.constants import (
    ASSIGNABLE_PLATFORM_PERMISSIONS,
    PLATFORM_PRINCIPAL_NAME_RE,
)
from app.modules.platform.models import PlatformPrincipal
from app.modules.system.models.tenant import Tenant  # noqa: F401

_BOOTSTRAP_ADVISORY_LOCK_ID = 0x504C41543541


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create the first platform-global principal after migration."
    )
    parser.add_argument("--principal-name", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument(
        "--permission",
        action="append",
        choices=sorted(ASSIGNABLE_PLATFORM_PERMISSIONS),
        dest="permissions",
        required=True,
        help="Explicit platform permission; repeat for multiple permissions.",
    )
    return parser.parse_args()


def _read_password() -> str:
    password = getpass("Platform password: ")
    confirmation = getpass("Confirm platform password: ")
    if password != confirmation:
        raise ValueError("password confirmation does not match")
    if len(password) < 12 or len(password.encode("utf-8")) > 72:
        raise ValueError("password must be at least 12 characters and at most 72 bytes")
    if not any(character.isalpha() for character in password) or not any(
        character.isdigit() for character in password
    ):
        raise ValueError("password must contain at least one letter and digit")
    return password


async def _create_first_principal(
    session: AsyncSession, arguments: argparse.Namespace, password: str
) -> PlatformPrincipal:
    principal_name = arguments.principal_name.strip().lower()
    if PLATFORM_PRINCIPAL_NAME_RE.fullmatch(principal_name) is None:
        raise ValueError("principal name format is invalid")
    display_name = arguments.display_name.strip()
    if (
        not display_name
        or len(display_name) > 100
        or any(not character.isprintable() for character in display_name)
    ):
        raise ValueError("display name must contain 1-100 characters")
    permissions = sorted(set(arguments.permissions or []))
    if not permissions or any(
        permission not in ASSIGNABLE_PLATFORM_PERMISSIONS for permission in permissions
    ):
        raise ValueError("at least one explicit platform permission is required")

    await session.execute(
        text("SELECT pg_advisory_xact_lock(:lock_id)"),
        {"lock_id": _BOOTSTRAP_ADVISORY_LOCK_ID},
    )
    existing = await session.scalar(select(PlatformPrincipal.principal_id).limit(1))
    if existing is not None:
        raise ValueError("a platform principal already exists")

    principal = PlatformPrincipal(
        principal_name=principal_name,
        display_name=display_name,
        hashed_password=get_password_hash(password),
        permissions=permissions,
    )
    session.add(principal)
    await session.flush()
    return principal


async def _bootstrap(arguments: argparse.Namespace, password: str) -> None:
    async with AsyncSessionLocal() as session:
        principal = await _create_first_principal(session, arguments, password)
        await session.commit()
        print(f"Created platform principal: {principal.principal_name}")


def main() -> None:
    arguments = _arguments()
    asyncio.run(_bootstrap(arguments, _read_password()))


if __name__ == "__main__":
    main()
