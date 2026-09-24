"""Offline CLI for the platform principal lifecycle.

Subcommands:
  create               Bootstrap the first independent platform principal.
  replace-permissions  Audited replacement of one principal's permissions.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from dataclasses import dataclass
from getpass import getpass

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants import STATUS_ENABLED
from app.core.exceptions import AuthenticationException, BusinessException
from app.core.security import get_password_hash, verify_password
from app.db.session import AsyncSessionLocal
from app.modules.platform.audit import (
    AuthorizedPlatformRequest,
    authorize_platform_request,
    persist_platform_completion,
)
from app.modules.platform.constants import (
    ASSIGNABLE_PLATFORM_PERMISSIONS,
    PLATFORM_PRINCIPAL_NAME_RE,
    PLATFORM_PRINCIPAL_PERMISSION_REPLACE,
)
from app.modules.platform.models import PlatformPrincipal
from app.modules.system.models.tenant import Tenant  # noqa: F401

_BOOTSTRAP_ADVISORY_LOCK_ID = 0x504C41543541
_SCRIPT_PATH = "tools/ops/platform_principal.py"
_PERMISSION_LOCK_NAMESPACE = "platform-principal-permissions"


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offline platform principal lifecycle CLI."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser(
        "create",
        help="Create the first platform-global principal after migration.",
    )
    create.add_argument("--principal-name", required=True)
    create.add_argument("--display-name", required=True)
    create.add_argument(
        "--permission",
        action="append",
        choices=sorted(ASSIGNABLE_PLATFORM_PERMISSIONS),
        dest="permissions",
        required=True,
        help="Explicit platform permission; repeat for multiple permissions.",
    )

    replace = subparsers.add_parser(
        "replace-permissions",
        help="Replace a platform principal's permissions with audit lineage.",
    )
    replace.add_argument("--principal-name", required=True)
    replace.add_argument(
        "--permission",
        action="append",
        choices=sorted(ASSIGNABLE_PLATFORM_PERMISSIONS),
        dest="permissions",
        required=True,
        help="Complete desired permission set; repeat for each permission.",
    )
    replace.add_argument("--reason", required=True)
    replace.add_argument("--ticket-id", required=True)
    replace.add_argument("--correlation-id", required=True)
    return parser.parse_args()


# ============ create：首个平台主体 bootstrap ============


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


# ============ replace-permissions：权限替换 ============


@dataclass(frozen=True, slots=True)
class PrincipalSnapshot:
    principal_id: int
    principal_name: str
    row_version: int

    @property
    def permissions(self) -> frozenset[str]:
        """Expose only the offline action to the shared audit authorizer."""
        return frozenset({PLATFORM_PRINCIPAL_PERMISSION_REPLACE})


def _read_current_password() -> str:
    password = getpass("Current platform password: ")
    if not password or len(password.encode("utf-8")) > 72:
        raise ValueError("current platform password is invalid")
    return password


def _normalize_arguments(
    arguments: argparse.Namespace,
) -> tuple[str, tuple[str, ...]]:
    principal_name = arguments.principal_name.strip().lower()
    if PLATFORM_PRINCIPAL_NAME_RE.fullmatch(principal_name) is None:
        raise ValueError("principal name format is invalid")
    permissions = tuple(sorted(set(arguments.permissions or [])))
    if not permissions or any(
        permission not in ASSIGNABLE_PLATFORM_PERMISSIONS for permission in permissions
    ):
        raise ValueError("at least one explicit platform permission is required")
    return principal_name, permissions


def _password_matches(password: str, hashed_password: str) -> bool:
    try:
        return verify_password(password, hashed_password)
    except (TypeError, ValueError):
        return False


async def _load_verified_snapshot(
    session: AsyncSession, *, principal_name: str, current_password: str
) -> PrincipalSnapshot:
    principal = await session.scalar(
        select(PlatformPrincipal).where(
            PlatformPrincipal.principal_name == principal_name
        )
    )
    if (
        principal is None
        or principal.status != STATUS_ENABLED
        or not _password_matches(current_password, principal.hashed_password)
    ):
        raise AuthenticationException(
            "平台账号或密码错误", error_code="PLATFORM_INVALID_CREDENTIALS"
        )
    return PrincipalSnapshot(
        principal_id=principal.principal_id,
        principal_name=principal.principal_name,
        row_version=principal.row_version,
    )


async def _authorize_permission_replacement(
    snapshot: PrincipalSnapshot,
    arguments: argparse.Namespace,
    *,
    persist=None,
) -> AuthorizedPlatformRequest:
    values = {
        "principal": snapshot,
        "permission": PLATFORM_PRINCIPAL_PERMISSION_REPLACE,
        "method": "CLI",
        "path": _SCRIPT_PATH,
        "reason": arguments.reason,
        "ticket_id": arguments.ticket_id,
        "correlation_id": arguments.correlation_id,
        "ip": None,
        "request_summary": None,
        "target_tenant_id": None,
    }
    if persist is not None:
        values["persist"] = persist
    return await authorize_platform_request(**values)


async def _apply_permission_replacement(
    session: AsyncSession,
    *,
    principal_id: int,
    expected_row_version: int,
    current_password: str,
    permissions: tuple[str, ...],
) -> bool:
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": f"{_PERMISSION_LOCK_NAMESPACE}:{principal_id}"},
    )
    principal = await session.scalar(
        select(PlatformPrincipal)
        .where(PlatformPrincipal.principal_id == principal_id)
        .with_for_update()
    )
    if (
        principal is None
        or principal.status != STATUS_ENABLED
        or principal.row_version != expected_row_version
        or not _password_matches(current_password, principal.hashed_password)
    ):
        raise BusinessException(
            code=409,
            message="平台主体在授权后发生变化，请重新执行",
            error_code="PLATFORM_PRINCIPAL_CHANGED",
        )
    desired = list(permissions)
    if principal.permissions == desired:
        return False
    principal.permissions = desired
    await session.flush()
    await session.refresh(principal)
    if principal.row_version <= expected_row_version:
        raise BusinessException(
            code=503,
            message="平台安全版本未更新",
            error_code="PLATFORM_PRINCIPAL_VERSION_NOT_BUMPED",
        )
    return True


async def _append_completion(
    authorization: AuthorizedPlatformRequest,
    *,
    status_code: int,
    duration_ms: int,
    affected_count: int | None,
) -> None:
    context = authorization.context
    summary = {"statusCode": status_code}
    if affected_count is not None:
        summary["affectedCount"] = affected_count
    values = {
        "authorization_audit_id": authorization.authorization_audit_id,
        "actor_principal_id": context.actor_principal_id,
        "actor_name": context.actor_name,
        "permission": PLATFORM_PRINCIPAL_PERMISSION_REPLACE,
        "method": "CLI",
        "path": authorization.audit_path,
        "reason": context.reason,
        "ticket_id": context.ticket_id,
        "correlation_id": context.correlation_id,
        "ip": None,
        "target_tenant_id": None,
        "status_code": status_code,
        "duration_ms": max(duration_ms, 0),
        "result_summary": summary,
    }
    last_error = None
    for _attempt in range(2):
        try:
            await persist_platform_completion(**values)
            return
        except Exception as exc:
            last_error = exc
    raise BusinessException(
        code=503,
        message="平台完成审计暂不可用",
        error_code="PLATFORM_AUDIT_UNAVAILABLE",
    ) from last_error


async def _replace(arguments: argparse.Namespace, current_password: str) -> bool:
    principal_name, permissions = _normalize_arguments(arguments)
    async with AsyncSessionLocal() as session:
        snapshot = await _load_verified_snapshot(
            session,
            principal_name=principal_name,
            current_password=current_password,
        )

    authorization = await _authorize_permission_replacement(snapshot, arguments)
    started = time.perf_counter()
    try:
        async with AsyncSessionLocal() as session:
            changed = await _apply_permission_replacement(
                session,
                principal_id=snapshot.principal_id,
                expected_row_version=snapshot.row_version,
                current_password=current_password,
                permissions=permissions,
            )
            await session.commit()
    except BusinessException as exc:
        await _append_completion(
            authorization,
            status_code=exc.code if 400 <= exc.code <= 599 else 500,
            duration_ms=int((time.perf_counter() - started) * 1000),
            affected_count=None,
        )
        raise
    except Exception:
        await _append_completion(
            authorization,
            status_code=500,
            duration_ms=int((time.perf_counter() - started) * 1000),
            affected_count=None,
        )
        raise

    await _append_completion(
        authorization,
        status_code=200,
        duration_ms=int((time.perf_counter() - started) * 1000),
        affected_count=int(changed),
    )
    return changed


def main() -> None:
    arguments = _arguments()
    if arguments.command == "create":
        asyncio.run(_bootstrap(arguments, _read_password()))
        return
    try:
        changed = asyncio.run(_replace(arguments, _read_current_password()))
    except BusinessException as exc:
        print(f"Permission replacement failed: {exc.error_code}", file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print(
            "Permission replacement failed: PLATFORM_PERMISSION_REPLACE_FAILED",
            file=sys.stderr,
        )
        raise SystemExit(1) from None
    outcome = "updated" if changed else "unchanged"
    print(f"Platform principal permissions: {outcome}")


if __name__ == "__main__":
    main()
